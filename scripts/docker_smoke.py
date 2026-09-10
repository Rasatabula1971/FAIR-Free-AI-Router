"""Exercise the disposable demo Compose stack over HTTP, including an API restart."""

import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


def request(path, *, key=None, payload=None, expected=200):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(BASE + path, data=data, headers=headers)
    try:
        with urlopen(req, timeout=5) as response:
            status, body = response.status, response.read()
    except HTTPError as error:
        status, body = error.code, error.read()
    assert status == expected, f"{path}: expected HTTP {expected}, got {status}"
    return json.loads(body)


def wait_ready():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            health = request("/healthz")
            assert health["status"] == "ok"
            assert health["live_inference_enabled"] is False
            return
        except (URLError, TimeoutError, ConnectionError):
            time.sleep(1)
    raise RuntimeError("Packaged API did not become ready within 60 seconds")


def restore_drill():
    """Disposable CI database only; never replace or clear the running source database."""
    compose = ["docker", "compose", "-f", "infra/docker/docker-compose.yml", "exec", "-T", "db"]

    def run(*args):
        result = subprocess.run(
            compose + list(args), cwd=ROOT, check=True, capture_output=True, text=True, timeout=60
        )
        return result.stdout.strip()

    def summary(name):
        return run(
            "psql",
            "-X",
            "-U",
            "fair",
            "-d",
            name,
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "SELECT version_num FROM alembic_version; SELECT stopped FROM system_state WHERE id='global'; SELECT count(*) FROM task_requests; SELECT count(*) FROM audit_events; SELECT sum(used) FROM provider_quota_states;",
        )

    before = summary("fair")
    run("pg_dump", "-U", "fair", "-d", "fair", "-Fc", "--file=/tmp/fair-smoke.dump")
    run("createdb", "-U", "fair", "-T", "template0", "fair_restore_verify")
    run(
        "pg_restore",
        "-U",
        "fair",
        "-d",
        "fair_restore_verify",
        "--exit-on-error",
        "--single-transaction",
        "/tmp/fair-smoke.dump",
    )
    assert summary("fair_restore_verify") == before
    assert summary("fair") == before
    assert before.splitlines()[1] == "t", "Restore must retain the maintenance stop"
    # Database-enforced append-only audit protection must survive restore as well.
    denied = subprocess.run(
        compose
        + [
            "psql",
            "-X",
            "-U",
            "fair",
            "-d",
            "fair_restore_verify",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "UPDATE audit_events SET event_type='tampered'",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert denied.returncode != 0 and "append-only" in denied.stderr


def main():
    key = json.loads(os.environ["FAIR_CLIENT_KEYS"])["smoke"]
    admin = os.environ["FAIR_ADMIN_KEY"]
    payload = {"client_id": "smoke", "task": "Hello"}
    wait_ready()
    assert request("/livez") == {"status": "alive"}
    assert request("/readyz") == {"status": "ready"}
    request("/v1/system/status", key=key, expected=403)
    status = request("/v1/system/status", key=admin)
    assert status["schema_current"] and status["credentials_configured"]
    assert status["configured_live_adapters"] == 0
    preflight = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "infra/docker/docker-compose.yml",
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "fair.operations.preflight",
            "--check-database",
            "--allow-demo",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(preflight.stdout)["status"] == "PASS"
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "infra/docker/docker-compose.yml",
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "from pathlib import Path; assert not Path('/app/secrets/build-context-canary').exists()",
        ],
        cwd=ROOT,
        check=True,
        timeout=15,
    )
    request("/v1/providers", expected=401)
    providers = request("/v1/providers", key=key)
    active = {p["provider_id"] for p in providers if p["status"] == "ACTIVE"}
    assert active == {"mock_primary", "mock_secondary"}
    request("/v1/system/scheduler", key=key, expected=403)
    scheduler = request("/v1/system/scheduler", key=admin)
    assert scheduler["scope"] == "process" and scheduler["queued"] == scheduler["active"] == 0
    denied = request("/v1/solve", key=key, payload=payload | {"priority": "P0"})
    assert denied["reason_code"] == "PRIORITY_NOT_ALLOWED" and denied["attempts"] == []
    result = request("/v1/solve", key=key, payload=payload)
    assert result["status"] == "ESCALATION_REQUIRED"
    assert result["paid_inference_executed"] is False
    assert result["output"] is None
    assert result["execution_kind"] == "PRIMARY"
    assert len(result["attempts"]) == 2
    assert {a["provider_id"] for a in result["attempts"]} == active
    history_path = f"/v1/requests/{result['request_id']}"
    assert request(history_path, key=key) == result
    audit = request(history_path + "/audit", key=key)
    assert audit
    assert [event["event_type"] for event in audit[:3]] == ["QUEUED", "SCHEDULED", "PROFILED"]
    feedback = request(
        "/v1/feedback",
        key=key,
        payload={"request_id": result["request_id"], "accepted": True},
        expected=409,
    )
    assert feedback["detail"] == "FEEDBACK_REQUIRES_ACCEPTED_PRIMARY_RESULT"
    metrics = request("/v1/models/performance", key=admin)
    assert metrics and all(
        "drift_state" in row and "observation_confidence" in row for row in metrics
    )
    request("/v1/system/stop", key=key, payload={}, expected=403)
    assert request("/v1/system/stop", key=admin, payload={})["stopped"] is True
    assert request("/readyz", expected=503) == {"status": "not_ready"}
    assert request("/livez") == {"status": "alive"}
    restore_drill()
    subprocess.run(
        ["docker", "compose", "-f", "infra/docker/docker-compose.yml", "restart", "api"],
        cwd=ROOT,
        check=True,
        timeout=60,
    )
    wait_ready()
    assert request(history_path, key=key) == result
    assert request(history_path + "/audit", key=key) == audit
    stopped = request("/v1/solve", key=key, payload=payload)
    assert stopped["reason_code"] == "SYSTEM_STOPPED"
    assert stopped["attempts"] == []
    assert stopped["paid_inference_executed"] is False
    recovery = request("/v1/system/providers/mock_primary/recovery", key=admin)
    assert recovery["state"]["used"] == 1
    request("/v1/system/providers/mock_primary/recovery", key=key, expected=403)
    denied_recovery = request(
        "/v1/system/providers/mock_primary/recover",
        key=admin,
        payload={
            "action": "clear_authentication",
            "expected_state": recovery["expected_state"],
            "review_reference": "ci-smoke",
        },
        expected=409,
    )
    assert denied_recovery["detail"] == "NO_RECOVERY_NEEDED"
    assert request("/v1/system/resume", key=admin, payload={})["stopped"] is False
    assert request("/readyz") == {"status": "ready"}
    resumed = request("/v1/solve", key=key, payload=payload)
    assert len(resumed["attempts"]) == 2
    assert resumed["status"] == "ESCALATION_REQUIRED"
    assert resumed["paid_inference_executed"] is False
    print(
        "Docker smoke passed: routing, readiness, preflight, backup/restore, audit protection, restart"
    )


if __name__ == "__main__":
    main()
