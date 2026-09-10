"""Guard a trusted provider adapter against accidental credential reflection."""

from pydantic import BaseModel

from fair.providers.base import (
    AuthenticationFailed,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)


class CredentialedAdapter:
    def __init__(self, provider_id, adapter, credential):
        if adapter.provider_id != provider_id:
            raise ValueError("Adapter identity mismatch")
        self.provider_id = provider_id
        self._adapter, self._credential = adapter, credential

    def _check(self, value):
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, str):
            if self._credential.get_secret_value() in value:
                raise AuthenticationFailed("CREDENTIAL_EXPOSURE_BLOCKED")
        elif isinstance(value, dict):
            for key, item in value.items():
                self._check(key)
                self._check(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._check(item)

    async def _call(self, method, *args):
        self._check(args)
        try:
            result = await method(*args)
        except AuthenticationFailed:
            raise AuthenticationFailed("AUTHENTICATION_FAILED") from None
        except QuotaExceeded as error:
            raise QuotaExceeded("QUOTA_EXHAUSTED", reset_at=error.reset_at) from None
        except RateLimited:
            raise RateLimited("RATE_LIMITED") from None
        except Exception:
            raise ProviderUnavailable("PROVIDER_UNAVAILABLE") from None
        self._check(result)
        return result

    async def complete(self, request):
        return await self._call(self._adapter.complete, request)

    async def health(self):
        return await self._call(self._adapter.health)

    async def quota(self):
        return await self._call(self._adapter.quota)

    async def list_models(self):
        return await self._call(self._adapter.list_models)
