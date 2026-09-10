import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import provider
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select

from apps.api.main import create_app
from fair.governor.recovery import ProviderRecovery, Recovery, RecoveryDenied, RequestRecovery
from fair.providers.mock import MockAdapter
from fair.quota.governor import QuotaGovernor
from fair.schemas.api import SolveRequest
from fair.schemas.db import (
    AuditEvent,
    ModelTaskPerformance,
    TaskRequest,
    utcnow,
)

NOW = 2_000_000_000.0


def change(recovery, action="clear_authentication", **values):
    return ProviderRecovery(
        action=action,
        expected_state=recovery.inspect_provider("a")["expected_state"],
        review_reference="private-review-reference",
        **values,
    )


def maintenance(make_router):
    router = make_router([(provider(request_limit=2), MockAdapter("a"))])
    router.quota.clock = lambda: NOW - 10
    router.quota.reserve(router.registry.providers["a"])
    router.quota.block_security("a")
    router.stopped = True
    return router, Recovery(router, clock=lambda: NOW)


def test_authentication_recovery_preserves_quota_other_blocks_and_audits(make_router):
    router, recovery = maintenance(make_router)
    router.quota.exhaust("a")
    router.quota.throttle("a")
    result = recovery.provider("a", change(recovery))
    assert not result["after"]["security_blocked"]
    assert result["after"]["used"] == 1 and result["after"]["exhausted"]
    assert result["after"]["blocked_until"] == result["before"]["blocked_until"]
    assert router.stopped
    with router.sessions() as session:
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "PROVIDER_RECOVERY")
        )
        assert audit.actor_id == "admin" and audit.payload_json == result
        assert "private-review-reference" not in json.dumps(audit.payload_json)
        assert not list(session.scalars(select(ModelTaskPerformance)))
    restarted = QuotaGovernor(router.settings, router.sessions, clock=lambda: NOW)
    assert not restarted.state("a").security_blocked and restarted.state("a").exhausted


@pytest.mark.parametrize("condition", ["running", "inflight", "stale", "unknown", "noop"])
def test_recovery_preconditions_are_fail_closed(make_router, condition):
    router, recovery = maintenance(make_router)
    request = change(recovery)
    if condition == "running":
        router.stopped = False
    elif condition == "inflight":
        router.inflight.add("active-request")
    elif condition == "stale":
        router.quota.throttle("a")
    elif condition == "noop":
        recovery.provider("a", request)
        request = change(recovery)
    before = recovery.inspect_provider("a")
    with pytest.raises(RecoveryDenied):
        recovery.provider("missing" if condition == "unknown" else "a", request)
    assert recovery.inspect_provider("a") == before


def test_stale_replay_cannot_reapply_and_audit_failure_rolls_back(make_router):
    router, recovery = maintenance(make_router)
    request = change(recovery)
    before = recovery.inspect_provider("a")

    def fail(mapper, connection, target):
        if target.event_type == "PROVIDER_RECOVERY":
            raise RuntimeError("audit unavailable")

    event.listen(AuditEvent, "before_insert", fail)
    try:
        with pytest.raises(RuntimeError):
            recovery.provider("a", request)
        assert recovery.inspect_provider("a") == before
    finally:
        event.remove(AuditEvent, "before_insert", fail)
    recovery.provider("a", request)
    with pytest.raises(RecoveryDenied, match="RECOVERY_STATE_CHANGED"):
        recovery.provider("a", request)


def test_rearm_preserves_quota_and_allows_only_one_probe(make_router):
    router, recovery = maintenance(make_router)
    recovery.provider("a", change(recovery))
    for _ in range(3):
        router.quota.failure("a")
    request = change(recovery, "rearm_circuit")
    recovery.provider("a", request)
    router.quota.clock = lambda: NOW
    spec = router.registry.providers["a"]
    assert router.quota.state("a").circuit_state == "OPEN"
    assert router.quota.reserve(spec)
    assert router.quota.state("a").circuit_state == "HALF_OPEN"
    assert not QuotaGovernor(router.settings, router.sessions, clock=lambda: NOW).reserve(spec)
    assert router.quota.state("a").used == 2


def test_quota_reset_requires_new_boundary_and_cannot_be_replayed(make_router):
    router, recovery = maintenance(make_router)
    router.quota.exhaust("a")
    reset = datetime.fromtimestamp(NOW - 5, UTC)
    recovery.provider("a", change(recovery, "confirm_quota_reset", observed_reset_at=reset))
    state = router.quota.state("a")
    assert state.used == 0 and not state.exhausted and state.security_blocked
    assert state.last_quota_reset_at == reset.timestamp()
    recovery.provider("a", change(recovery))
    router.quota.clock = lambda: NOW - 2
    router.quota.reserve(router.registry.providers["a"])
    with pytest.raises(RecoveryDenied, match="ALREADY_ACCOUNTED"):
        recovery.provider("a", change(recovery, "confirm_quota_reset", observed_reset_at=reset))
    assert router.quota.state("a").used == 1


@pytest.mark.parametrize("offset", [1, -86401, -10, -11])
def test_invalid_or_prior_reservation_reset_cannot_clear_usage(make_router, offset):
    router, recovery = maintenance(make_router)
    with pytest.raises(RecoveryDenied):
        recovery.provider(
            "a",
            change(
                recovery,
                "confirm_quota_reset",
                observed_reset_at=datetime.fromtimestamp(NOW + offset, UTC),
            ),
        )
    assert router.quota.state("a").used == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "enable"},
        {"observed_reset_at": datetime.now(UTC)},
        {"action": "confirm_quota_reset"},
        {"expected_state": "bad"},
        {"review_reference": " "},
        {"action": "confirm_quota_reset", "observed_reset_at": datetime.now()},
    ],
)
def test_invalid_recovery_contract(changes):
    with pytest.raises(ValidationError):
        ProviderRecovery(
            **(
                {
                    "action": "clear_authentication",
                    "expected_state": "0" * 64,
                    "review_reference": "review",
                }
                | changes
            )
        )


def test_recovery_does_not_enable_disabled_or_paid_provider(make_router):
    router, recovery = maintenance(make_router)
    router.registry.providers["a"].status = "DISABLED"
    router.registry.providers["a"].current_access_cost_usd = 1
    recovery.provider("a", change(recovery))
    assert router.quota.effective_status(router.registry.providers["a"]) == "DISABLED"


def seed_requests(router):
    with router.sessions.begin() as session:
        for index, status in enumerate(
            ["QUEUED", "EXECUTING", "PROFILED", "ACCEPTED", "CANCELLED", "FAILED", "QUEUED"]
        ):
            session.add(
                TaskRequest(
                    id=f"request-{index}",
                    client_id="alice",
                    status=status,
                    profile_json={},
                    created_at=utcnow() + timedelta(hours=1 if index == 6 else -1),
                    result_json={"output": "accepted-private"} if status == "ACCEPTED" else None,
                )
            )


def test_interrupted_request_recovery_is_bounded_idempotent_and_preserves_quota(make_router):
    router, _ = maintenance(make_router)
    recovery = Recovery(router)
    seed_requests(router)
    request = RequestRecovery(created_before=utcnow(), review_reference="incident", limit=2)
    assert recovery.requests(request)["recovered"] == 2
    assert recovery.requests(request)["recovered"] == 1
    assert recovery.requests(request)["recovered"] == 0
    assert router.quota.state("a").used == 1
    with router.sessions() as session:
        assert (
            session.get(TaskRequest, "request-0").result_json["reason_code"]
            == "PROCESS_INTERRUPTED"
        )
        assert session.get(TaskRequest, "request-3").result_json["output"] == "accepted-private"
        assert session.get(TaskRequest, "request-4").status == "CANCELLED"
        assert session.get(TaskRequest, "request-6").status == "QUEUED"
        assert (
            len(
                list(
                    session.scalars(
                        select(AuditEvent).where(AuditEvent.event_type == "REQUEST_RECOVERED")
                    )
                )
            )
            == 3
        )


async def test_stop_does_not_allow_recovery_while_provider_cleanup_runs(make_router):
    started, release = asyncio.Event(), asyncio.Event()

    class Held(MockAdapter):
        async def complete(self, value):
            started.set()
            await release.wait()
            return await super().complete(value)

    router = make_router([(provider(), Held("a"))])
    active = asyncio.create_task(router.solve(SolveRequest(client_id="alice", task="x")))
    await started.wait()
    router.stopped = True
    recovery = Recovery(router)
    with pytest.raises(RecoveryDenied, match="IN_FLIGHT"):
        recovery.requests(RequestRecovery(created_before=utcnow(), review_reference="incident"))
    release.set()
    await active
    assert not router.inflight


def test_http_recovery_authentication_and_client_history_isolation(make_router):
    router, _ = maintenance(make_router)
    seed_requests(router)
    with TestClient(create_app(router, {"alice": "a", "bob": "b"}, "admin")) as client:
        admin = {"X-API-Key": "admin"}
        path = "/v1/system/providers/a/recovery"
        assert client.get(path, headers={"X-API-Key": "a"}).status_code == 403
        state = client.get(path, headers=admin).json()
        body = {
            "action": "clear_authentication",
            "expected_state": state["expected_state"],
            "review_reference": "review",
        }
        assert (
            client.post(
                "/v1/system/providers/a/recover", headers={"X-API-Key": "a"}, json=body
            ).status_code
            == 403
        )
        assert (
            client.post("/v1/system/providers/a/recover", headers=admin, json=body).status_code
            == 200
        )
        assert (
            client.post("/v1/system/providers/a/recover", headers=admin, json=body).status_code
            == 409
        )
        request = {"created_before": utcnow().isoformat(), "review_reference": "incident"}
        assert (
            client.post(
                "/v1/system/requests/recover", headers={"X-API-Key": "a"}, json=request
            ).status_code
            == 403
        )
        result = client.post("/v1/system/requests/recover", headers=admin, json=request).json()
        assert result["recovered"] == 3 and "alice" not in json.dumps(result)
        for suffix in ("", "/audit"):
            assert (
                client.get(
                    "/v1/requests/request-0" + suffix, headers={"X-API-Key": "b"}
                ).status_code
                == 404
            )
            assert (
                client.get(
                    "/v1/requests/request-0" + suffix, headers={"X-API-Key": "a"}
                ).status_code
                == 200
            )
