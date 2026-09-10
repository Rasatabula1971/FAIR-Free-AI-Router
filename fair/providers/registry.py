from sqlalchemy import select

from fair.governor.policy import admit_provider
from fair.providers.base import ProviderAdapter
from fair.schemas.db import Model, Provider, ProviderQuotaState, SystemState, utcnow
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
        """Future live factories receive only their scoped SecretStr after admission."""
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

    def persist(self, sessions):
        """Reconcile trusted config without resetting durable quota or security state."""
        with sessions.begin() as session:
            if session.get(SystemState, "global") is None:
                session.add(SystemState(id="global", stopped=False))
            for row in session.scalars(select(Provider)):
                if row.provider_id not in self.providers:
                    row.status = "DISABLED"
            for spec in self.providers.values():
                values = spec.model_dump(exclude={"models"})
                row = session.get(Provider, spec.provider_id)
                if row is None:
                    row = Provider(**values)
                    session.add(row)
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
                row.updated_at = utcnow()
                session.flush()
                if session.get(ProviderQuotaState, spec.provider_id) is None:
                    session.add(ProviderQuotaState(provider_id=spec.provider_id))
                configured = {m.model_id for m in spec.models}
                for model in session.scalars(
                    select(Model).where(Model.provider_id == spec.provider_id)
                ):
                    if model.model_id not in configured:
                        model.active = False
                for model in spec.models:
                    session.merge(
                        Model(provider_id=spec.provider_id, **model.model_dump(mode="json"))
                    )
