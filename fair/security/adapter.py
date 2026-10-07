"""Guard a trusted provider adapter against accidental credential reflection."""

import logging
import re

from pydantic import BaseModel

from fair.providers.base import (
    AccessDenied,
    AuthenticationFailed,
    BillingViolation,
    MalformedResponse,
    ModelUnavailable,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimited,
    RequestNotSupported,
    StructuredOutputRejected,
)

logger = logging.getLogger(__name__)

# A FAIR-authored diagnostic code, e.g. HTTP_503 or INVALID_CHAT_COMPLETION.
# Upstream response text is never shaped like this, so a message that matches
# cannot be carrying a provider body, a URL or a credential. Anything else is
# replaced with a generic code rather than being passed on.
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _safe_code(error, fallback):
    code = str(error)
    return code if _SAFE_CODE.fullmatch(code) else fallback


class CredentialedAdapter:
    def __init__(self, provider_id, adapter, credential):
        if adapter.provider_id != provider_id:
            raise ValueError("Adapter identity mismatch")
        self.provider_id = provider_id
        self._adapter, self._credential = adapter, credential
        self.live_inference = getattr(adapter, "live_inference", False)

    _MAX_CHECK_DEPTH = 32

    def _check(self, value, depth=0):
        if depth > self._MAX_CHECK_DEPTH:
            raise AuthenticationFailed("CREDENTIAL_CHECK_DEPTH_EXCEEDED")
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            if self._credential.get_secret_value() in value:
                # Only the provider identifier is logged; the credential value is never
                # emitted. The suppression below sits on its own line so a formatter
                # cannot detach it from the call the way a trailing comment was.
                # nosemgrep
                logger.error("Credential exposure blocked for provider %s", self.provider_id)
                raise AuthenticationFailed("CREDENTIAL_EXPOSURE_BLOCKED")
        elif isinstance(value, dict):
            for key, item in value.items():
                self._check(key, depth + 1)
                self._check(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._check(item, depth + 1)

    async def _call(self, method, *args):
        self._check(args)
        try:
            result = await method(*args)
        except BillingViolation:
            logger.error("Billing violation from provider %s", self.provider_id)
            raise BillingViolation("PROVIDER_REPORTED_NONZERO_OR_INVALID_COST") from None
        except ModelUnavailable as error:
            code = _safe_code(error, "REVIEWED_MODEL_UNAVAILABLE")
            logger.warning("Model unavailable at provider %s: %s", self.provider_id, code)
            raise ModelUnavailable(code) from None
        except AccessDenied:
            logger.warning("Access denied by provider %s", self.provider_id)
            raise AccessDenied("PROVIDER_ACCESS_DENIED") from None
        except AuthenticationFailed:
            logger.error("Authentication failed for provider %s", self.provider_id)
            raise AuthenticationFailed("AUTHENTICATION_FAILED") from None
        except QuotaExceeded as error:
            logger.info("Quota exceeded for provider %s", self.provider_id)
            # The scope travels with the error: dropped here, a limit on one model
            # reaches the router as a limit on the whole provider.
            scoped = error.model_scoped is True
            raise QuotaExceeded(
                "MODEL_QUOTA_EXHAUSTED" if scoped else "QUOTA_EXHAUSTED",
                reset_at=error.reset_at,
                model_scoped=scoped,
            ) from None
        except RateLimited as error:
            logger.info("Rate limited by provider %s", self.provider_id)
            scoped = error.model_scoped is True
            raise RateLimited(
                "MODEL_RATE_LIMITED" if scoped else "RATE_LIMITED",
                retry_after=error.retry_after,
                model_scoped=scoped,
            ) from None
        except RequestNotSupported as error:
            code = _safe_code(error, "REQUEST_NOT_SUPPORTED")
            raise RequestNotSupported(code) from None
        except StructuredOutputRejected as error:
            # Must precede MalformedResponse: re-raised as its parent, the subclass
            # the router classifies on is lost and a model's wrong-shaped JSON is
            # charged to the provider again.
            code = _safe_code(error, "PROVIDER_REJECTED_GENERATED_SCHEMA")
            logger.info("Provider %s rejected a generated schema: %s", self.provider_id, code)
            raise StructuredOutputRejected(code) from None
        except MalformedResponse as error:
            # FAIR's own adapters compose these codes; they distinguish a bad
            # envelope from an oversized body from a budget violation, which
            # the attempt log needs. Only the code survives, never upstream text.
            code = _safe_code(error, "MALFORMED_PROVIDER_RESPONSE")
            logger.warning("Malformed response from provider %s: %s", self.provider_id, code)
            raise MalformedResponse(code) from None
        except ProviderUnavailable as error:
            code = _safe_code(error, "PROVIDER_UNAVAILABLE")
            logger.warning("Provider %s unavailable: %s", self.provider_id, code)
            raise ProviderUnavailable(code) from None
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

    def safe_diagnostics(self):
        method = getattr(self._adapter, "safe_diagnostics", None)
        if not callable(method):
            return {}
        result = method()
        self._check(result)
        return result

    async def close(self):
        if hasattr(self._adapter, "close"):
            await self._adapter.close()

    async def health(self):
        return await self._call(self._adapter.health)

    async def quota(self):
        return await self._call(self._adapter.quota)

    async def list_models(self):
        return await self._call(self._adapter.list_models)
