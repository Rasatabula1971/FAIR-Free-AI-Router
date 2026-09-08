from fair.schemas.domain import AccessClass, ProviderSpec, ProviderState


class AdmissionDenied(Exception):
    pass


def admit_provider(spec: ProviderSpec) -> None:
    """Explicit checks remain effective under python -O; unknown costs fail closed."""
    eligible = (
        spec.access_class in set(AccessClass)
        and spec.current_access_cost_usd == 0
        and not spec.requires_paid_subscription
        and not spec.requires_credit_purchase
        and not spec.auto_billing_required
        and spec.programmatic_access
        and spec.production_eligibility
        and spec.status in {ProviderState.ACTIVE, ProviderState.QUOTA_PRESSURE}
        and (spec.access_class == AccessClass.FREE_LOCAL or spec.terms_last_verified is not None)
    )
    if not eligible:
        raise AdmissionDenied("Provider does not satisfy free-only admission policy")
