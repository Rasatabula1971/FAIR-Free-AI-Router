import json

import pytest
from conftest import provider
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fair.config import RoutingSettings
from fair.governor.policy import AdmissionDenied
from fair.providers.base import (
    AuthenticationFailed,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest
from fair.schemas.db import Base
from fair.schemas.domain import NormalizedModelRequest
from fair.security.credentials import APIKeys, CredentialConfigurationError, ProviderCredentials

SECRET = 'sentinel-provider-"-credential'


@pytest.mark.parametrize(
    "clients,admin",
    [
        ({"a": "same", "b": "same"}, "admin"),
        ({"a": "same"}, "same"),
        ({"a": ""}, "admin"),
        ({"a": None}, "admin"),
        ({"a": "has space"}, "admin"),
        ({"": "key"}, "admin"),
        ({"a": "key"}, "bad\nkey"),
        ([], "admin"),
        ({"a": "key"}, None),
    ],
)
def test_invalid_and_overlapping_keys_fail_closed(clients, admin):
    with pytest.raises(CredentialConfigurationError) as error:
        APIKeys(clients, admin)
    assert "same" not in str(error.value) and "key" not in str(error.value)


def test_keys_are_snapshot_digests_and_roles_do_not_overlap():
    clients = {"alice": "private-client", "bob": "other-client"}
    keys = APIKeys(clients, "private-admin")
    clients["alice"] = "replacement"
    assert "private-client" not in repr(vars(keys)) and "private-admin" not in repr(vars(keys))
    assert keys.authenticate(["private-client"]) == "alice"
    assert keys.authenticate(["replacement"]) is None
    assert keys.authenticate(["private-admin"]) is None
    assert keys.authenticate(["private-client"], administrator=True) is None
    assert keys.authenticate(["private-admin"], administrator=True) == "admin"
    assert keys.authenticate(["private-client", "private-admin"]) is None
    assert keys.authenticate(["non-ascii-界"]) is None
    assert APIKeys({}, "").authenticate(["anything"], administrator=True) is None


def test_secret_files_load_rotate_on_reload_and_fail_closed(tmp_path, monkeypatch):
    for variable in (
        "FAIR_CLIENT_KEYS",
        "FAIR_ADMIN_KEY",
        "FAIR_CLIENT_KEYS_FILE",
        "FAIR_ADMIN_KEY_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)
    clients, admin = tmp_path / "clients.json", tmp_path / "admin.json"
    clients.write_text(json.dumps({"alice": "old-key"}))
    admin.write_text(json.dumps("admin-key"))
    monkeypatch.setenv("FAIR_CLIENT_KEYS_FILE", str(clients))
    monkeypatch.setenv("FAIR_ADMIN_KEY_FILE", str(admin))
    old = APIKeys.load()
    clients.write_text(json.dumps({"alice": "new-key"}))
    assert old.authenticate(["old-key"]) == "alice"
    assert APIKeys.load().authenticate(["old-key"]) is None
    assert APIKeys.load().authenticate(["new-key"]) == "alice"
    monkeypatch.setenv("FAIR_CLIENT_KEYS", '{"alice":"secret"}')
    with pytest.raises(CredentialConfigurationError):
        APIKeys.load()
    monkeypatch.delenv("FAIR_CLIENT_KEYS")
    for content in [
        '{"alice":"secret","alice":"another"}',
        '"secret"',
        "bad-secret-json",
        "x" * 65537,
    ]:
        clients.write_text(content)
        with pytest.raises(CredentialConfigurationError) as error:
            APIKeys.load()
        assert "secret" not in str(error.value) and str(tmp_path) not in str(error.value)


def test_duplicate_headers_and_validation_errors_do_not_echo_private_input(make_router):
    with TestClient(create_app(make_router(), {"alice": "client-key"}, "admin-key")) as client:
        headers = [("X-API-Key", "client-key"), ("X-API-Key", "admin-key")]
        assert client.get("/v1/providers", headers=headers).status_code == 401
        assert client.post("/v1/system/stop", headers=headers).status_code == 403
        response = client.post(
            "/v1/solve",
            headers={"X-API-Key": "client-key"},
            json={"client_id": "alice", "task": "x", "api_key": SECRET},
        )
        assert response.status_code == 422 and SECRET not in response.text
        response = client.post(
            "/v1/solve",
            headers={"X-API-Key": "client-key", "Content-Type": "application/json"},
            content='{"secret":"private-malformed',
        )
        assert response.status_code == 422 and "private-malformed" not in response.text


def test_provider_credentials_are_scoped_redacted_and_governed(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_A_KEY", SECRET)
    monkeypatch.setenv("TEST_PROVIDER_B_KEY", "other-secret")
    credentials = ProviderCredentials({"a": "TEST_PROVIDER_A_KEY"})
    handle = credentials.for_provider("a")
    assert SECRET not in repr(handle) and SECRET not in str(handle)
    for identity in ("b", "TEST_PROVIDER_B_KEY", "FAIR_ADMIN_KEY"):
        with pytest.raises(CredentialConfigurationError):
            credentials.for_provider(identity)
    registry = Registry()
    called = []
    with pytest.raises(AdmissionDenied):
        registry.register_credentialed(
            provider(current_access_cost_usd=1), lambda secret: called.append(secret), credentials
        )
    assert called == []
    registry.register_credentialed(provider(), lambda secret: MockAdapter("a"), credentials)
    assert SECRET not in registry.providers["a"].model_dump_json()


@pytest.mark.parametrize(
    "mode", ["ok", "echo", "authentication", "quota", "rate", "generic", "input"]
)
async def test_guarded_adapter_never_serializes_credentials(make_router, monkeypatch, caplog, mode):
    monkeypatch.setenv("TEST_PROVIDER_A_KEY", SECRET)
    normalized = []

    class Adapter(MockAdapter):
        async def complete(self, value):
            normalized.append(value.model_dump_json())
            return await super().complete(value)

    errors = {
        "authentication": AuthenticationFailed(SECRET),
        "quota": QuotaExceeded(SECRET),
        "rate": RateLimited(SECRET),
        "generic": RuntimeError(SECRET),
    }
    adapter = Adapter("a", text=SECRET if mode == "echo" else "4", error=errors.get(mode))
    base = make_router()
    registry = Registry()
    registry.register_credentialed(
        provider(), lambda secret: adapter, ProviderCredentials({"a": "TEST_PROVIDER_A_KEY"})
    )
    router = Router(registry, RoutingSettings(), base.thresholds, base.sessions)
    result = await router.solve(
        SolveRequest(
            client_id="alice",
            task=SECRET if mode == "input" else "Calculate",
            validation={"kind": "arithmetic", "expression": "2+2"},
        )
    )
    assert (result.status == "ACCEPTED") == (mode == "ok")
    assert SECRET not in result.model_dump_json() and SECRET not in caplog.text
    assert all(SECRET not in value for value in normalized)
    with router.sessions() as session:
        for mapper in Base.registry.mappers:
            for row in session.scalars(select(mapper.class_)):
                values = {column.key: getattr(row, column.key) for column in mapper.columns}
                serialized = json.dumps(values, default=str)
                assert SECRET not in serialized
                assert json.dumps(SECRET)[1:-1] not in serialized
                assert not {"api_key", "credential", "authorization"}.intersection(values)
    if mode in {"echo", "input", "authentication"}:
        assert router.quota.state("a").security_blocked


async def test_discovery_and_direct_errors_are_also_guarded(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_A_KEY", SECRET)
    registry = Registry()
    adapter = MockAdapter(
        "a",
        error=RuntimeError(SECRET),
        models=[provider(models=[{"model_id": SECRET, "context_window": 10}]).models[0]],
    )
    registry.register_credentialed(
        provider(), lambda secret: adapter, ProviderCredentials({"a": "TEST_PROVIDER_A_KEY"})
    )
    guarded = registry.adapters["a"]
    with pytest.raises(AuthenticationFailed):
        await guarded.list_models()
    with pytest.raises(ProviderUnavailable) as error:
        await guarded.complete(
            NormalizedModelRequest(
                task="x", model_id="model", request_id="1", client_id="alice", task_class="general"
            )
        )
    assert SECRET not in str(error.value)
