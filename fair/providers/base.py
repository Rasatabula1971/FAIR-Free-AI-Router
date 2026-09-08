from typing import Protocol

from fair.schemas.domain import (
    ModelDescriptor,
    NormalizedModelRequest,
    NormalizedModelResponse,
    ProviderHealth,
    QuotaSnapshot,
)


class ProviderError(Exception):
    """Never serialize exception text: upstream errors may contain credentials."""


class RateLimited(ProviderError):
    pass


class QuotaExceeded(ProviderError):
    def __init__(self, message="", *, reset_at: float | None = None):
        super().__init__(message)
        self.reset_at = reset_at


class ProviderUnavailable(ProviderError):
    pass


class AuthenticationFailed(ProviderError):
    pass


class MalformedResponse(ProviderError):
    pass


class ProviderAdapter(Protocol):
    provider_id: str

    async def health(self) -> ProviderHealth: ...

    async def quota(self) -> QuotaSnapshot: ...

    async def list_models(self) -> list[ModelDescriptor]: ...

    async def complete(self, request: NormalizedModelRequest) -> NormalizedModelResponse: ...
