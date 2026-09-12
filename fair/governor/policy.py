import logging

from fair.schemas.domain import AccessClass, ProviderSpec, ProviderState

logger = logging.getLogger(__name__)


class AdmissionDenied(Exception):
    pass


def admit_provider(spec: ProviderSpec) -> None:
    """Explicit checks remain effective under python -O; unknown costs fail closed."""
    checks = {
        "access_class": spec.access_class in set(AccessClass),
        "zero_cost": spec.current_access_cost_usd == 0,
        "no_paid_subscription": not spec.requires_paid_subscription,
        "no_credit_purchase": not spec.requires_credit_purchase,
        "no_auto_billing": not spec.auto_billing_required,
        "programmatic_access": spec.programmatic_access,
        "production_eligible": spec.production_eligibility,
        "status_active": spec.status in {ProviderState.ACTIVE, ProviderState.QUOTA_PRESSURE},
        "terms_verified": spec.access_class == AccessClass.FREE_LOCAL
        or spec.terms_last_verified is not None,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise AdmissionDenied(
            f"Provider {spec.provider_id} failed admission: {', '.join(failed)}"
        )
