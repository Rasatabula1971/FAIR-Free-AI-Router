"""Guard a trusted provider adapter against accidental credential reflection."""

from pydantic import BaseModel

from fair.providers.base import (
    AuthenticationFailed,
    BillingViolation,
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
        self.live_inference = getattr(adapter, "live_inference", False)

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
        except BillingViolation:
            raise BillingViolation("PROVIDER_REPORTED_NONZERO_OR_INVALID_COST") from None
        except AuthenticationFailed:
            raise AuthenticationFailed("AUTHENTICATION_FAILED") from None
        except QuotaExceeded as error:
            raise QuotaExceeded("QUOTA_EXHAUSTED", reset_at=error.reset_at) from None
        except RateLimited as error:
            raise RateLimited("RATE_LIMITED", retry_after=error.retry_after) from None
        except Exception:
            raise ProviderUnavailable("PROVIDER_UNAVAILABLE") from None
        self._check(result)
        return result

    async def complete(self, request):
        return await self._call(self._adapter.complete, request)

    def check_admission(self):
        try:
            if hasattr(self._adapter, "check_admission"):
                self._adapter.check_admission()
        except Exception:
            raise AuthenticationFailed("AUTHENTICATION_FAILED") from None

    async def close(self):
        if hasattr(self._adapter, "close"):
            await self._adapter.close()

    async def health(self):
        return await self._call(self._adapter.health)

    async def quota(self):
        return await self._call(self._adapter.quota)

    async def list_models(self):
        return await self._call(self._adapter.list_models)
