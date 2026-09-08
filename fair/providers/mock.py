from fair.schemas.domain import (
    ModelDescriptor,
    NormalizedModelRequest,
    NormalizedModelResponse,
    ProviderHealth,
    QuotaSnapshot,
)


class MockAdapter:
    """Offline fixture. Not an AI model and never represents verified intelligence."""

    def __init__(
        self, provider_id: str, text: str = "Offline mock response", error=None, models=None
    ):
        self.provider_id = provider_id
        self.text = text
        self.error = error
        self.calls = 0
        self.models = models or []

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id, state="ACTIVE", source="OFFLINE_FIXTURE"
        )

    async def quota(self) -> QuotaSnapshot:
        return QuotaSnapshot(provider_id=self.provider_id)

    async def list_models(self) -> list[ModelDescriptor]:
        return [model.model_copy(deep=True) for model in self.models]

    async def complete(self, request: NormalizedModelRequest) -> NormalizedModelResponse:
        self.calls += 1
        if self.error:
            raise self.error
        return NormalizedModelResponse(
            provider_id=self.provider_id, model_id=request.model_id, text=self.text
        )
