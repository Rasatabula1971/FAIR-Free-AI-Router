"""Process-local credential boundaries; never pass these objects to models or ORM rows."""

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from pydantic import SecretStr


class CredentialConfigurationError(ValueError):
    pass


def secret_config(value_name, file_name, default):
    try:
        value, path = os.environ.get(value_name), os.environ.get(file_name)
        if value is not None and path is not None:
            raise ValueError()
        if path is not None:
            with Path(path).open("rb") as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError()
            value = data.decode("utf-8")
        if value is None:
            return default
        if len(value.encode("utf-8")) > 65536:
            raise ValueError()

        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError()
                result[key] = item
            return result

        return json.loads(value, object_pairs_hook=unique)
    except Exception:
        raise CredentialConfigurationError("Invalid credential configuration") from None


def valid_secret(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and not any(c.isspace() for c in value)


class APIKeys:
    def __init__(self, clients, admin):
        if not isinstance(clients, dict) or len(clients) > 1000 or not isinstance(admin, str):
            raise CredentialConfigurationError("Invalid credential configuration")
        self._clients = {}
        self._admin = None
        for identity, key in clients.items():
            if (
                not isinstance(identity, str)
                or not 1 <= len(identity) <= 128
                or not valid_secret(key)
            ):
                raise CredentialConfigurationError("Invalid credential configuration")
            digest = hashlib.sha256(key.encode()).digest()
            if digest in self._clients:
                raise CredentialConfigurationError("Ambiguous credential configuration")
            self._clients[digest] = identity
        if admin:
            if not valid_secret(admin):
                raise CredentialConfigurationError("Invalid credential configuration")
            self._admin = hashlib.sha256(admin.encode()).digest()
            if self._admin in self._clients:
                raise CredentialConfigurationError("Ambiguous credential configuration")

    @classmethod
    def load(cls, clients=None, admin=None):
        if clients is None:
            clients = secret_config("FAIR_CLIENT_KEYS", "FAIR_CLIENT_KEYS_FILE", {})
        if admin is None:
            # File contains a JSON string, allowing unambiguous whitespace handling.
            if "FAIR_ADMIN_KEY_FILE" in os.environ:
                admin = secret_config("FAIR_ADMIN_KEY", "FAIR_ADMIN_KEY_FILE", "")
            else:
                admin = os.environ.get("FAIR_ADMIN_KEY", "")
        return cls(clients, admin)

    def authenticate(self, headers, *, administrator=False):
        if len(headers) != 1 or not valid_secret(headers[0]):
            return None
        digest = hashlib.sha256(headers[0].encode()).digest()
        if administrator:
            return (
                "admin"
                if self._admin is not None and hmac.compare_digest(digest, self._admin)
                else None
            )
        for candidate, identity in self._clients.items():
            if hmac.compare_digest(digest, candidate):
                return identity
        return None


class ProviderCredentials:
    """Resolve only operator-mapped environment variables for a trusted adapter factory.

    Factories receive one SecretStr, not the resolver or the entire environment. There are
    no live adapter factories yet; this boundary is tested for their later integration.
    """

    def __init__(self, bindings):
        if not isinstance(bindings, dict) or any(
            not isinstance(provider, str)
            or not provider
            or not isinstance(variable, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", variable)
            for provider, variable in bindings.items()
        ):
            raise CredentialConfigurationError("Invalid provider credential bindings")
        self._bindings = dict(bindings)

    def for_provider(self, provider_id):
        variable = self._bindings.get(provider_id)
        value = os.environ.get(variable) if variable else None
        if not valid_secret(value):
            raise CredentialConfigurationError("Provider credential unavailable")
        return SecretStr(value)
