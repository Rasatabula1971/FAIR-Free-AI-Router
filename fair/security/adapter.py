"""Guard a trusted provider adapter against accidental credential reflection."""

import logging

from pydantic import BaseModel

from fair.providers.base import (
    AuthenticationFailed,
    BillingViolation,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
)

logger = logging.getLogger(__name__)


class CredentialedAdapter:
    def __init__(self, provider_id, adapter, credential):
        if adapter.provider_id != provider_id:
            raise ValueError("Adapter identity mismatch")
        self.provider_id = provider_id
        self._adapter, self._credential = adapter, credential
        self.live_inference = getattr(adapter, "live_inference", False)

    _MAX_CHECK_DEPTH = 32

    def _check(self, value, _depth=0):
        if _depth > self._MAX_CHECK_DEPTH:
            raise AuthenticationFailed("CREDENTIAL_CHECK_DEPTH_EXCEEDED")
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            if self._credential.get_secret_value() in value:
                logger.error("Credential exposure blocked for provider %s", self.provider_id)
                raise AuthenticationFailed("CREDENTIAL_EXPOSURE_BLOCKED")
        elif isinstance(value, dict):
            for key, item in value.items():
                self._check(key, _depth + 1)
                self._check(item, _depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._check(item, _depth + 1)

    async def _call(self, method, *args):
        self._check(args)
        try:
            result = await method(*args)
        except BillingViolation:
            logger.error("Billing violation from provider %s", self.provider_id)
            raise BillingViolation("PROVIDER_REPORTED_NONZERO_OR_INVALID_COST") from None
        except AuthenticationFailed:
            logger.error("Authentication failed for provider %s", self.provider_id)
            raise AuthenticationFailed("AUTHENTICATION_FAILED") from None
        except QuotaExceeded as error:
            logger.info("Quota exceeded for provider %s", self.provider_id)
            raise QuotaExceeded("QUOTA_EXHAUSTED", reset_at=error.reset_at) from None
        except RateLimited as error:
            logger.info("Rate limited by provider %s", self.provider_id)
            raise RateLimited("RATE_LIMITED", retry_after=error.retry_after) from None
        except Exception:
            logger.warning("Provider %s unavailable", self.provider_id)
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
