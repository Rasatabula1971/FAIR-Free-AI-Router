"""Tests for the optional authenticated FAIR HTTP service."""

import json

import pytest
from fastapi.testclient import TestClient

from fair import FAIR
from fair.providers.mock import MockAdapter
from fair.schemas.domain import ProviderSpec
from fair.service import app as service
from fair.service.__main__ import _port
from fair.service.app import SERVICE_MODEL_ID, create_app, parse_client_keys

CORP_KEY = "corp-client-key-0123456789"
VIDEO_KEY = "video-client-key-0123456789"
ADMIN_KEY = "admin-service-key-01234567890123456789"


def _spec(**overrides):
    values = {
        "provider_id": "mock",
        "access_class": "FREE_LOCAL",
        "status": "ACTIVE",
        "current_access_cost_usd": 0,
        "requires_paid_subscription": False,
        "requires_credit_purchase": False,
        "auto_billing_required": False,
        "programmatic_access": True,
        "production_eligibility": True,
        "request_limit": 10,
        "request_limit_window": "DAILY_UTC",
        "models": [
            {
                "model_id": "model",
                "context_window": 32768,
                "capabilities": {"reasoning", "coding", "structured_output"},
            }
        ],
    }
    return ProviderSpec(**(values | overrides))


def _fair(text="345", *, shared_quota_path=None):
    return FAIR(
        providers=[(_spec(), MockAdapter("mock", text=text))],
        application_id="fair-service",
        shared_quota_path=shared_quota_path,
    )


def _headers(key=CORP_KEY):
    return {"Authorization": f"Bearer {key}"}


class TestServiceAuth:
    def test_missing_bearer_key_is_rejected(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.get("/v1/models")
        assert response.status_code == 401

    def test_invalid_bearer_key_is_rejected(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.get("/v1/models", headers=_headers("not-a-valid-client-key"))
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "[]",
            "{}",
            '{"corp":"short"}',
            '{"":"0123456789abcdef"}',
            '{"corp":"0123456789abcdef","video":"0123456789abcdef"}',
            '{"corp":"replace-with-random-client-key"}',
        ],
    )
    def test_invalid_client_configuration_fails_closed(self, raw):
        with pytest.raises(ValueError):
            parse_client_keys(raw)

    def test_valid_client_configuration(self):
        assert parse_client_keys(json.dumps({"corp": CORP_KEY})) == {"corp": CORP_KEY}

    @pytest.mark.asyncio
    async def test_non_ascii_bearer_token_is_rejected_without_crashing(self):
        resolver = service._client_resolver({"corp": CORP_KEY})
        with pytest.raises(Exception) as captured:
            await resolver("Bearer ☃")
        assert getattr(captured.value, "status_code", None) == 401

    def test_admin_key_must_not_equal_a_client_key(self):
        with pytest.raises(ValueError, match="distinct"):
            create_app(_fair(), {"corp": CORP_KEY}, admin_key=CORP_KEY)


class TestServiceEndpoints:
    def test_health_is_local_safe_and_does_not_require_client_key(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {
                "status": "ok",
                "providers": 1,
                "admissible_providers": 1,
                "routable_providers": 1,
            }

    def test_models_exposes_router_not_provider_credentials(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            payload = client.get("/v1/models", headers=_headers()).json()
        assert payload["data"][0]["id"] == SERVICE_MODEL_ID
        encoded = json.dumps(payload)
        assert CORP_KEY not in encoded

    def test_fair_native_solve(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post(
                "/v1/fair/solve",
                headers=_headers(),
                json={
                    "task": "What is 15*23?",
                    "validation": {"kind": "arithmetic", "expression": "15*23"},
                },
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ACCEPTED"
        assert payload["output"] == "345"

    def test_client_id_cannot_be_spoofed_in_native_request(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post(
                "/v1/fair/solve",
                headers=_headers(),
                json={
                    "task": "15*23",
                    "client_id": "video",
                    "validation": {"kind": "arithmetic", "expression": "15*23"},
                },
            )
        assert response.status_code == 422

    def test_authenticated_callers_get_separate_quota_attribution(self, tmp_path):
        path = str(tmp_path / "quota.sqlite3")
        app = create_app(
            _fair(shared_quota_path=path),
            {"corp": CORP_KEY, "youtube-production": VIDEO_KEY},
        )
        request = {
            "task": "15*23",
            "validation": {"kind": "arithmetic", "expression": "15*23"},
        }
        with TestClient(app) as client:
            assert (
                client.post("/v1/fair/solve", headers=_headers(), json=request).status_code == 200
            )
            assert (
                client.post("/v1/fair/solve", headers=_headers(VIDEO_KEY), json=request).status_code
                == 200
            )
            usage = client.get("/v1/fair/quota", headers=_headers()).json()
        pool = usage["pools"][0]
        assert pool["applications"] == {"corp": 1, "youtube-production": 1}
        assert pool["used"] == 2

    def test_provider_status_endpoint(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            payload = client.get("/v1/fair/providers", headers=_headers()).json()
        assert payload["providers"][0]["provider_id"] == "mock"
        assert payload["skipped"] == {}

    def test_health_is_not_ready_when_all_providers_are_security_blocked(self):
        fair = _fair()
        fair._router.quota.block_security("mock")
        app = create_app(fair, {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "no_routable_providers"
        assert payload["routable_providers"] == 0

    def test_admin_api_is_disabled_without_admin_key(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post(
                "/v1/fair/admin/providers/mock/resume",
                headers=_headers(),
            )
        assert response.status_code == 404

    def test_admin_can_resume_only_with_the_admin_key(self):
        fair = _fair()
        fair._router.quota.block_security("mock")
        app = create_app(fair, {"corp": CORP_KEY}, admin_key=ADMIN_KEY)
        with TestClient(app) as client:
            denied = client.post(
                "/v1/fair/admin/providers/mock/resume",
                headers=_headers(),
            )
            assert denied.status_code == 401
            resumed = client.post(
                "/v1/fair/admin/providers/mock/resume",
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "ACTIVE"


class TestOpenAICompatibility:
    def test_structured_chat_completion_uses_fair_validation(self):
        app = create_app(_fair('{"answer":345}'), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers=_headers(),
                json={
                    "model": SERVICE_MODEL_ID,
                    "messages": [{"role": "user", "content": "Return the answer as JSON"}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "answer",
                            "schema": {
                                "type": "object",
                                "properties": {"answer": {"type": "integer"}},
                                "required": ["answer"],
                                "additionalProperties": False,
                            },
                        },
                    },
                },
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["choices"][0]["message"]["content"] == '{"answer":345}'
        assert payload["fair"]["verification_state"] == "STRUCTURE_VALIDATED"

    def test_json_object_response_format_is_supported(self):
        app = create_app(_fair('{"answer":345}'), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers=_headers(),
                json={
                    "model": SERVICE_MODEL_ID,
                    "messages": [{"role": "user", "content": "Return JSON"}],
                    "response_format": {"type": "json_object"},
                },
            )
        assert response.status_code == 200

    def test_free_form_chat_fails_closed_instead_of_returning_unverified_text(self):
        app = create_app(_fair("plausible but unverified answer"), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers=_headers(),
                json={
                    "model": SERVICE_MODEL_ID,
                    "messages": [{"role": "user", "content": "Tell me something"}],
                },
            )
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "fair_escalation_required"

    @pytest.mark.parametrize(
        ("field", "value", "detail"),
        [
            ("model", "some-provider-model", "Use model"),
            ("stream", True, "Streaming is not supported"),
        ],
    )
    def test_unsupported_chat_modes_are_explicit(self, field, value, detail):
        body = {
            "model": SERVICE_MODEL_ID,
            "messages": [{"role": "user", "content": "15*23"}],
        }
        body[field] = value
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post("/v1/chat/completions", headers=_headers(), json=body)
        assert response.status_code == 400
        assert detail in response.json()["detail"]

    def test_invalid_response_format_is_rejected(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers=_headers(),
                json={
                    "model": SERVICE_MODEL_ID,
                    "messages": [{"role": "user", "content": "hello"}],
                    "response_format": {"type": "xml"},
                },
            )
        assert response.status_code == 400

    def test_malformed_json_schema_is_rejected(self):
        app = create_app(_fair(), {"corp": CORP_KEY})
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers=_headers(),
                json={
                    "model": SERVICE_MODEL_ID,
                    "messages": [{"role": "user", "content": "hello"}],
                    "response_format": {"type": "json_schema", "json_schema": {"name": "x"}},
                },
            )
        assert response.status_code == 400


class TestServiceConfiguration:
    def test_quota_pool_json_validation(self):
        assert service._parse_quota_pool_ids('{"mock":"account-a"}') == {"mock": "account-a"}
        with pytest.raises(ValueError):
            service._parse_quota_pool_ids("[]")
        with pytest.raises(ValueError):
            service._parse_quota_pool_ids("{bad")

    def test_app_factory_reads_service_settings_without_exposing_keys(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            'FAIR_SERVICE_CLIENTS=\'{"corp":"corp-client-key-0123456789"}\'\n'
            "FAIR_CONFIRMED_FREE_PROVIDERS=groq\n"
            'FAIR_QUOTA_POOL_IDS=\'{"mock":"account-a"}\'\n',
            encoding="utf-8",
        )
        captured = {}

        def fake_fair(**kwargs):
            captured.update(kwargs)
            return _fair()

        monkeypatch.setattr(service, "FAIR", fake_fair)
        monkeypatch.setenv("FAIR_ENV_FILE", str(env_file))
        app = service.create_app_from_env()
        assert app.title == "FAIR Free AI Router"
        assert captured["confirmed_free_providers"] == {"groq"}
        assert captured["quota_pool_ids"] == {"mock": "account-a"}

    def test_port_parser(self, monkeypatch):
        monkeypatch.setenv("FAIR_SERVICE_PORT", "8123")
        assert _port() == 8123
        monkeypatch.setenv("FAIR_SERVICE_PORT", "0")
        with pytest.raises(SystemExit):
            _port()
        monkeypatch.setenv("FAIR_SERVICE_PORT", "not-a-number")
        with pytest.raises(SystemExit):
            _port()
