"""Credential boundary tests.

Every path here is a way a secret could be accepted when it should not be, be
confused with another client's, or leak between providers. The module was at
18% coverage.
"""

import json
import sys

import pytest
from pydantic import SecretStr

from fair.security.credentials import (
    APIKeys,
    CredentialConfigurationError,
    ProviderCredentials,
    secret_config,
    valid_secret,
)

CLIENT_KEY = "client-key-0123456789"
ADMIN_KEY = "admin-key-0123456789"


# ── valid_secret ────────────────────────────────────────────────────────


class TestValidSecret:
    @pytest.mark.parametrize("value", ["k", "a" * 4096, "sk-abc.DEF-123"])
    def test_accepts_a_plausible_secret(self, value):
        assert valid_secret(value)

    @pytest.mark.parametrize(
        "value",
        ["", "a" * 4097, "has space", "trailing ", "tab\there", "new\nline", None, 123, b"bytes"],
    )
    def test_rejects_anything_else(self, value):
        assert not valid_secret(value)


# ── secret_config ───────────────────────────────────────────────────────


class TestSecretConfig:
    def test_returns_the_default_when_nothing_is_set(self, monkeypatch):
        monkeypatch.delenv("FAIR_TEST_VALUE", raising=False)
        monkeypatch.delenv("FAIR_TEST_FILE", raising=False)
        assert secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {"fallback": 1}) == {
            "fallback": 1
        }

    def test_reads_json_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("FAIR_TEST_VALUE", json.dumps({"c": CLIENT_KEY}))
        monkeypatch.delenv("FAIR_TEST_FILE", raising=False)
        assert secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {}) == {"c": CLIENT_KEY}

    def test_reads_json_from_a_file(self, monkeypatch, tmp_path):
        path = tmp_path / "keys.json"
        path.write_text(json.dumps({"c": CLIENT_KEY}), encoding="utf-8")
        monkeypatch.delenv("FAIR_TEST_VALUE", raising=False)
        monkeypatch.setenv("FAIR_TEST_FILE", str(path))
        assert secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {}) == {"c": CLIENT_KEY}

    def test_setting_both_the_value_and_the_file_is_ambiguous(self, monkeypatch, tmp_path):
        path = tmp_path / "keys.json"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("FAIR_TEST_VALUE", "{}")
        monkeypatch.setenv("FAIR_TEST_FILE", str(path))
        with pytest.raises(CredentialConfigurationError):
            secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {})

    def test_an_oversized_file_is_refused(self, monkeypatch, tmp_path):
        path = tmp_path / "big.json"
        path.write_text('{"k": "' + "x" * 65536 + '"}', encoding="utf-8")
        monkeypatch.delenv("FAIR_TEST_VALUE", raising=False)
        monkeypatch.setenv("FAIR_TEST_FILE", str(path))
        with pytest.raises(CredentialConfigurationError):
            secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {})

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "Windows caps an environment variable at 32,767 characters, so a value over "
            "the 65,536-byte limit cannot be set there; the file path below covers the "
            "same limit on every platform"
        ),
    )
    def test_an_oversized_value_is_refused(self, monkeypatch):
        monkeypatch.setenv("FAIR_TEST_VALUE", '{"k": "' + "x" * 65536 + '"}')
        monkeypatch.delenv("FAIR_TEST_FILE", raising=False)
        with pytest.raises(CredentialConfigurationError):
            secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {})

    def test_a_missing_file_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FAIR_TEST_VALUE", raising=False)
        monkeypatch.setenv("FAIR_TEST_FILE", str(tmp_path / "absent.json"))
        with pytest.raises(CredentialConfigurationError):
            secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {})

    def test_malformed_json_is_refused(self, monkeypatch):
        monkeypatch.setenv("FAIR_TEST_VALUE", "{not json")
        monkeypatch.delenv("FAIR_TEST_FILE", raising=False)
        with pytest.raises(CredentialConfigurationError):
            secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {})

    def test_a_duplicated_json_key_is_refused(self, monkeypatch):
        """Last-one-wins would silently drop one client's mapping."""
        monkeypatch.setenv("FAIR_TEST_VALUE", '{"c": "one", "c": "two"}')
        monkeypatch.delenv("FAIR_TEST_FILE", raising=False)
        with pytest.raises(CredentialConfigurationError):
            secret_config("FAIR_TEST_VALUE", "FAIR_TEST_FILE", {})


# ── APIKeys ─────────────────────────────────────────────────────────────


class TestAPIKeysConstruction:
    @pytest.mark.parametrize(
        "clients,admin",
        [
            ("not a dict", ""),
            ({}, None),
            ({"c": "has space"}, ""),
            ({"c": ""}, ""),
            ({"": CLIENT_KEY}, ""),
            ({"c" * 129: CLIENT_KEY}, ""),
            ({1: CLIENT_KEY}, ""),
            ({"c": CLIENT_KEY}, "admin key with space"),
        ],
    )
    def test_invalid_configuration_is_refused(self, clients, admin):
        with pytest.raises(CredentialConfigurationError, match="Invalid credential"):
            APIKeys(clients, admin)

    def test_too_many_clients_is_refused(self):
        with pytest.raises(CredentialConfigurationError):
            APIKeys({f"c{index}": f"key-{index}" for index in range(1001)}, "")

    def test_two_clients_sharing_a_key_is_ambiguous(self):
        with pytest.raises(CredentialConfigurationError, match="Ambiguous"):
            APIKeys({"one": CLIENT_KEY, "two": CLIENT_KEY}, "")

    def test_an_admin_key_matching_a_client_key_is_ambiguous(self):
        with pytest.raises(CredentialConfigurationError, match="Ambiguous"):
            APIKeys({"one": CLIENT_KEY}, CLIENT_KEY)

    def test_configured_requires_both_clients_and_an_admin(self):
        assert APIKeys({"one": CLIENT_KEY}, ADMIN_KEY).configured
        assert not APIKeys({"one": CLIENT_KEY}, "").configured
        assert not APIKeys({}, ADMIN_KEY).configured


class TestAPIKeysAuthenticate:
    def _keys(self):
        return APIKeys({"one": CLIENT_KEY, "two": "second-key-0123456789"}, ADMIN_KEY)

    def test_a_known_client_key_resolves_to_its_identity(self):
        assert self._keys().authenticate([CLIENT_KEY]) == "one"
        assert self._keys().authenticate(["second-key-0123456789"]) == "two"

    def test_an_unknown_key_is_not_authenticated(self):
        assert self._keys().authenticate(["nope-0123456789"]) is None

    def test_the_admin_key_is_not_a_client_key(self):
        assert self._keys().authenticate([ADMIN_KEY]) is None

    def test_the_admin_key_authenticates_as_administrator(self):
        assert self._keys().authenticate([ADMIN_KEY], administrator=True) == "admin"

    def test_a_client_key_does_not_authenticate_as_administrator(self):
        assert self._keys().authenticate([CLIENT_KEY], administrator=True) is None

    def test_administrator_authentication_fails_when_no_admin_is_configured(self):
        keys = APIKeys({"one": CLIENT_KEY}, "")
        assert keys.authenticate([ADMIN_KEY], administrator=True) is None

    @pytest.mark.parametrize("headers", [[], [CLIENT_KEY, CLIENT_KEY], [""], ["has space"], [None]])
    def test_exactly_one_well_formed_header_is_required(self, headers):
        assert self._keys().authenticate(headers) is None


class TestAPIKeysLoad:
    def test_load_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("FAIR_CLIENT_KEYS", json.dumps({"one": CLIENT_KEY}))
        monkeypatch.setenv("FAIR_ADMIN_KEY", ADMIN_KEY)
        monkeypatch.delenv("FAIR_CLIENT_KEYS_FILE", raising=False)
        monkeypatch.delenv("FAIR_ADMIN_KEY_FILE", raising=False)
        keys = APIKeys.load()
        assert keys.configured
        assert keys.authenticate([CLIENT_KEY]) == "one"

    def test_an_admin_key_file_holds_a_json_string(self, monkeypatch, tmp_path):
        path = tmp_path / "admin.json"
        path.write_text(json.dumps(ADMIN_KEY), encoding="utf-8")
        monkeypatch.setenv("FAIR_CLIENT_KEYS", json.dumps({"one": CLIENT_KEY}))
        monkeypatch.setenv("FAIR_ADMIN_KEY_FILE", str(path))
        monkeypatch.delenv("FAIR_CLIENT_KEYS_FILE", raising=False)
        monkeypatch.delenv("FAIR_ADMIN_KEY", raising=False)
        assert APIKeys.load().authenticate([ADMIN_KEY], administrator=True) == "admin"

    def test_load_with_nothing_configured_is_unconfigured(self, monkeypatch):
        for name in (
            "FAIR_CLIENT_KEYS",
            "FAIR_CLIENT_KEYS_FILE",
            "FAIR_ADMIN_KEY",
            "FAIR_ADMIN_KEY_FILE",
        ):
            monkeypatch.delenv(name, raising=False)
        assert not APIKeys.load().configured

    def test_explicit_arguments_override_the_environment(self, monkeypatch):
        monkeypatch.setenv("FAIR_CLIENT_KEYS", json.dumps({"env": "env-key-0123456789"}))
        keys = APIKeys.load(clients={"explicit": CLIENT_KEY}, admin=ADMIN_KEY)
        assert keys.authenticate([CLIENT_KEY]) == "explicit"
        assert keys.authenticate(["env-key-0123456789"]) is None


# ── ProviderCredentials ─────────────────────────────────────────────────


class TestProviderCredentialBindings:
    @pytest.mark.parametrize(
        "bindings",
        [
            "not a dict",
            {"p": "lowercase"},
            {"p": "1LEADING_DIGIT"},
            {"p": "HAS-DASH"},
            {"p": ""},
            {"": "VAR"},
            {"p": "A" * 129},
            {1: "VAR"},
            {"p": 2},
        ],
    )
    def test_invalid_bindings_are_refused(self, bindings):
        with pytest.raises(CredentialConfigurationError, match="Invalid provider"):
            ProviderCredentials(bindings)

    def test_two_providers_sharing_a_variable_is_ambiguous(self):
        with pytest.raises(CredentialConfigurationError, match="Ambiguous"):
            ProviderCredentials({"one": "SHARED_KEY", "two": "SHARED_KEY"})

    def test_a_valid_binding_resolves_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_ONE_KEY", CLIENT_KEY)
        resolver = ProviderCredentials({"one": "PROVIDER_ONE_KEY"})
        assert resolver.for_provider("one").get_secret_value() == CLIENT_KEY

    def test_an_unmapped_provider_gets_nothing(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_ONE_KEY", CLIENT_KEY)
        resolver = ProviderCredentials({"one": "PROVIDER_ONE_KEY"})
        with pytest.raises(CredentialConfigurationError, match="unavailable"):
            resolver.for_provider("two")

    def test_an_unset_variable_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("PROVIDER_ONE_KEY", raising=False)
        with pytest.raises(CredentialConfigurationError, match="unavailable"):
            ProviderCredentials({"one": "PROVIDER_ONE_KEY"}).for_provider("one")

    def test_a_malformed_environment_value_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("PROVIDER_ONE_KEY", "has space")
        with pytest.raises(CredentialConfigurationError, match="unavailable"):
            ProviderCredentials({"one": "PROVIDER_ONE_KEY"}).for_provider("one")

    def test_bindings_are_copied(self):
        bindings = {"one": "PROVIDER_ONE_KEY"}
        resolver = ProviderCredentials(bindings)
        bindings["one"] = "PROVIDER_OTHER_KEY"
        assert resolver._bindings == {"one": "PROVIDER_ONE_KEY"}


class TestProviderCredentialEnvFile:
    BINDINGS = {"one": "PROVIDER_ONE_KEY", "two": "PROVIDER_TWO_KEY"}

    def _resolver(self, tmp_path, body, encoding="utf-8"):
        path = tmp_path / ".env"
        path.write_text(body, encoding=encoding)
        return ProviderCredentials.from_env_file(self.BINDINGS, path)

    def test_an_env_file_never_touches_the_process_environment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PROVIDER_ONE_KEY", raising=False)
        resolver = self._resolver(tmp_path, f"PROVIDER_ONE_KEY={CLIENT_KEY}\n")
        assert resolver.for_provider("one").get_secret_value() == CLIENT_KEY
        import os

        assert "PROVIDER_ONE_KEY" not in os.environ

    def test_an_env_file_shadows_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROVIDER_TWO_KEY", "from-environment-0123")
        resolver = self._resolver(tmp_path, f"PROVIDER_ONE_KEY={CLIENT_KEY}\n")
        with pytest.raises(CredentialConfigurationError, match="unavailable"):
            resolver.for_provider("two")

    @pytest.mark.parametrize(
        "body",
        [
            "PROVIDER_ONE_KEY={key}\n",
            "PROVIDER_ONE_KEY='{key}'\n",
            'PROVIDER_ONE_KEY="{key}"\n',
            "PROVIDER_ONE_KEY = {key}\n",
            "# a comment\n\nPROVIDER_ONE_KEY={key}\n",
            "PROVIDER_ONE_KEY={key}   # trailing comment\n",
            'PROVIDER_ONE_KEY="{key}"  # quoted then comment\n',
            "UNRELATED_SETTING=whatever\nPROVIDER_ONE_KEY={key}\n",
        ],
    )
    def test_supported_env_file_forms(self, tmp_path, body):
        resolver = self._resolver(tmp_path, body.format(key=CLIENT_KEY))
        assert resolver.for_provider("one").get_secret_value() == CLIENT_KEY

    def test_a_byte_order_mark_is_tolerated(self, tmp_path):
        resolver = self._resolver(
            tmp_path, f"PROVIDER_ONE_KEY={CLIENT_KEY}\n", encoding="utf-8-sig"
        )
        assert resolver.for_provider("one").get_secret_value() == CLIENT_KEY

    @pytest.mark.parametrize(
        "body",
        [
            "lowercase_key=value\n",
            "PROVIDER_ONE_KEY\n",
            f"PROVIDER_ONE_KEY={CLIENT_KEY}\nPROVIDER_ONE_KEY=other\n",
            f"PROVIDER_ONE_KEY='{CLIENT_KEY}\n",
            f"PROVIDER_ONE_KEY={CLIENT_KEY}\x00\n",
            "PROVIDER_ONE_KEY=has space\n",
        ],
    )
    def test_a_malformed_env_file_is_refused(self, tmp_path, body):
        with pytest.raises(CredentialConfigurationError, match="Invalid provider env file"):
            self._resolver(tmp_path, body)

    def test_an_oversized_env_file_is_refused(self, tmp_path):
        with pytest.raises(CredentialConfigurationError, match="Invalid provider env file"):
            self._resolver(tmp_path, "PROVIDER_ONE_KEY=" + "x" * 65536 + "\n")

    def test_a_missing_env_file_is_refused(self, tmp_path):
        with pytest.raises(CredentialConfigurationError, match="Invalid provider env file"):
            ProviderCredentials.from_env_file(self.BINDINGS, tmp_path / "absent")

    def test_an_empty_value_is_ignored_rather_than_stored(self, tmp_path):
        resolver = self._resolver(tmp_path, "PROVIDER_ONE_KEY=\n")
        with pytest.raises(CredentialConfigurationError, match="unavailable"):
            resolver.for_provider("one")

    def test_only_bound_variables_are_retained(self, tmp_path):
        resolver = self._resolver(
            tmp_path, f"OTHER_PROVIDER_KEY=not-ours-0123\nPROVIDER_ONE_KEY={CLIENT_KEY}\n"
        )
        assert set(resolver._values) == {"PROVIDER_ONE_KEY"}

    def test_a_resolved_value_is_a_secret(self, tmp_path):
        resolver = self._resolver(tmp_path, f"PROVIDER_ONE_KEY={CLIENT_KEY}\n")
        value = resolver.for_provider("one")
        assert isinstance(value, SecretStr)
        assert CLIENT_KEY not in repr(value)
