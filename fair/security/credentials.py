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

    @property
    def configured(self):
        return bool(self._clients) and self._admin is not None


class ProviderCredentials:
    """Resolve only operator-mapped credentials for a trusted adapter factory.

    Factories receive one SecretStr, not the resolver or the entire environment.
    Explicit env files are isolated snapshots and never modify the process environment.
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
        if len(set(bindings.values())) != len(bindings):
            raise CredentialConfigurationError("Ambiguous provider credential bindings")
        self._bindings = dict(bindings)
        self._values = None

    @classmethod
    def from_env_file(cls, bindings, path):
        try:
            resolver = cls(bindings)
            with Path(path).open("rb") as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError()
            source = data.decode("utf-8-sig")
            if any(ord(char) < 32 and char not in "\r\n\t" for char in source):
                raise ValueError()
            seen, values = set(), {}
            for line in source.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.fullmatch(r"([A-Z][A-Z0-9_]{0,127})\s*=\s*(.*)", line)
                if match is None or match[1] in seen:
                    raise ValueError()
                variable, value = match.groups()
                seen.add(variable)
                if value.startswith(("'", '"')):
                    quoted = re.fullmatch(r"(['\"])(.*?)\1(?:[ \t]+#.*)?", value)
                    if quoted is None:
                        raise ValueError()
                    value = quoted[2]
                else:
                    value = re.split(r"[ \t]+#", value, maxsplit=1)[0].strip()
                # Ignore unrelated settings; never retain other providers' secrets.
                if variable in bindings.values() and value:
                    if not valid_secret(value):
                        raise ValueError()
                    values[variable] = SecretStr(value)
            resolver._values = values
            return resolver
        except Exception:
            raise CredentialConfigurationError("Invalid provider env file") from None

    def for_provider(self, provider_id):
        variable = self._bindings.get(provider_id)
        if self._values is not None:
            value = self._values.get(variable)
            if value is None:
                raise CredentialConfigurationError("Provider credential unavailable")
            return value
        value = os.environ.get(variable) if variable else None
        if not valid_secret(value):
            raise CredentialConfigurationError("Provider credential unavailable")
        return SecretStr(value)
