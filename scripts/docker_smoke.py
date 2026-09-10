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


def main():
    key = json.loads(os.environ["FAIR_CLIENT_KEYS"])["smoke"]
    admin = os.environ["FAIR_ADMIN_KEY"]
    payload = {"client_id": "smoke", "task": "Hello"}
    wait_ready()
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
    assert len(result["attempts"]) == 2
    assert {a["provider_id"] for a in result["attempts"]} == active
    history_path = f"/v1/requests/{result['request_id']}"
    assert request(history_path, key=key) == result
    audit = request(history_path + "/audit", key=key)
    assert audit
    assert [event["event_type"] for event in audit[:3]] == ["QUEUED", "SCHEDULED", "PROFILED"]
    request("/v1/system/stop", key=key, payload={}, expected=403)
    assert request("/v1/system/stop", key=admin, payload={})["stopped"] is True
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
    resumed = request("/v1/solve", key=key, payload=payload)
    assert len(resumed["attempts"]) == 2
    assert resumed["status"] == "ESCALATION_REQUIRED"
    assert resumed["paid_inference_executed"] is False
    print(
        "Docker smoke passed: authentication, offline routing, history, audit, stop/resume, restart"
    )


if __name__ == "__main__":
    main()
