from fair.governor.policy import admit_provider
from fair.providers.base import ProviderAdapter
from fair.schemas.domain import ProviderSpec
from fair.security.adapter import CredentialedAdapter
from fair.security.credentials import CredentialConfigurationError


class Registry:
    def __init__(self):
        self.providers: dict[str, ProviderSpec] = {}
        self.adapters: dict[str, ProviderAdapter] = {}

    def register(self, spec: ProviderSpec, adapter: ProviderAdapter | None = None):
        if spec.provider_id in self.providers:
            raise ValueError("Duplicate provider")
        if spec.status == "ACTIVE":
            admit_provider(spec)
        if adapter and adapter.provider_id != spec.provider_id:
            raise ValueError("Adapter identity mismatch")
        self.providers[spec.provider_id] = spec.model_copy(deep=True)
        if adapter:
            self.adapters[spec.provider_id] = adapter

    def register_credentialed(self, spec, factory, credentials):
        """Live factories receive only their scoped SecretStr after admission."""
        admit_provider(spec)
        if spec.provider_id in self.providers:
            raise ValueError("Duplicate provider")
        credential = credentials.for_provider(spec.provider_id)
        try:
            adapter = factory(credential)
            guarded = CredentialedAdapter(spec.provider_id, adapter, credential)
        except Exception:
            raise CredentialConfigurationError("Provider adapter initialization failed") from None
        self.register(spec, guarded)
