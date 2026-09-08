from fair.schemas.domain import NormalizedModelRequest, NormalizedModelResponse


class MockAdapter:
    """Offline fixture. Not an AI model and never represents verified intelligence."""

    def __init__(self, provider_id: str, text: str = "Offline mock response", error=None):
        self.provider_id = provider_id
        self.text = text
        self.error = error
        self.calls = 0

    async def complete(self, request: NormalizedModelRequest) -> NormalizedModelResponse:
        self.calls += 1
        if self.error:
            raise self.error
        return NormalizedModelResponse(
            provider_id=self.provider_id, model_id=request.model_id, text=self.text
        )
