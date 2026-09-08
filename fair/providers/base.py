from typing import Protocol

from fair.schemas.domain import NormalizedModelRequest, NormalizedModelResponse


class ProviderError(Exception):
    """Never serialize exception text: upstream errors may contain credentials."""


class RateLimited(ProviderError):
    pass


class QuotaExceeded(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class AuthenticationFailed(ProviderError):
    pass


class MalformedResponse(ProviderError):
    pass


class ProviderAdapter(Protocol):
    provider_id: str

    async def complete(self, request: NormalizedModelRequest) -> NormalizedModelResponse: ...
