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
    """A limit was hit and will clear by itself.

    ``model_scoped`` says the provider counts this limit per model, so its other
    models are unaffected. It is only ever set on the provider's own word -- an
    error that names the model or a per-model quota. Anything less is False: the
    scope is unknown, and benching the whole provider is the safe reading of that.
    """

    def __init__(self, message="", *, retry_after=None, model_scoped=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.model_scoped = model_scoped


class BillingViolation(ProviderError):
    """Unexpected/unknown cost: block the offending provider and withhold its response."""


class QuotaExceeded(ProviderError):
    """An allowance is spent until it resets; ``model_scoped`` as on RateLimited."""

    def __init__(self, message="", *, reset_at: float | None = None, model_scoped=False):
        super().__init__(message)
        self.reset_at = reset_at
        self.model_scoped = model_scoped


class ProviderUnavailable(ProviderError):
    pass


class AuthenticationFailed(ProviderError):
    pass


class MalformedResponse(ProviderError):
    pass


class RequestNotSupported(MalformedResponse):
    """The request cannot use this route; provider health is not implicated."""


class StructuredOutputRejected(MalformedResponse):
    """A provider's own schema validator refused the model's generation.

    The route works and the credential is good: the model wrote JSON that does not
    match the schema it was sent, which is the finding fair.quality.engine reports
    as SCHEMA_FAILURE. Provider health is not implicated.
    """


class ModelUnavailable(AuthenticationFailed):
    """A reviewed model is absent or changed; the provider credential is not implicated."""


class AccessDenied(AuthenticationFailed):
    """HTTP 403: this request was denied; it is not proof the credential is invalid."""


class ProviderAdapter(Protocol):
    provider_id: str

    async def health(self) -> ProviderHealth: ...

    async def quota(self) -> QuotaSnapshot: ...

    async def list_models(self) -> list[ModelDescriptor]: ...

    async def complete(self, request: NormalizedModelRequest) -> NormalizedModelResponse: ...
