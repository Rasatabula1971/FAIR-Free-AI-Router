"""Fail-closed review checks shared by registration, selection and dispatch."""

from datetime import timedelta

from fair.schemas.domain import AccessClass
from fair.schemas.qualification import FreeStatus

REVIEW_MAX_AGE = timedelta(days=30)


def qualified(spec, now):
    evidence = spec.qualification
    # Local transports retain their existing local-weight and cloud-exclusion controls.
    if evidence is None:
        return spec.access_class == AccessClass.FREE_LOCAL
    expected = {
        AccessClass.FREE_LOCAL: FreeStatus.LOCAL_COMPUTE,
        AccessClass.FREE_RECURRING: FreeStatus.VERIFIED_FREE_PLAN,
        AccessClass.FREE_DYNAMIC: FreeStatus.VERIFIED_ZERO_PRICE_MODEL,
    }
    if (
        evidence.provider_id != spec.provider_id
        or evidence.free_status != expected.get(spec.access_class)
        or evidence.production_allowed is not True
        or evidence.billing_enabled is not False
        or evidence.payment_method_required is not False
        or evidence.payment_method_present is None
        or evidence.can_auto_bill is not False
        or not all(
            (
                evidence.reviewer_reference,
                evidence.billing_reference,
                evidence.terms_reference,
                evidence.privacy_reference,
                evidence.limits_reference,
            )
        )
        or evidence.reviewed_at is None
        or evidence.expires_at is None
    ):
        return False
    if not (
        now - REVIEW_MAX_AGE
        <= evidence.reviewed_at
        <= now
        < evidence.expires_at
        <= evidence.reviewed_at + REVIEW_MAX_AGE
    ):
        return False
    reviews = {model.model_id: model for model in evidence.models}
    if len(reviews) != len(evidence.models):
        return False
    active = [model for model in spec.models if model.active]
    if not active:
        return False
    for model in active:
        review = reviews.get(model.model_id)
        if (
            review is None
            or review.model_revision != model.model_revision
            or review.input_price_per_million != 0
            or review.output_price_per_million != 0
            or review.request_price != 0
            or not review.pricing_reference
            or review.paid_tools_enabled is not False
            or review.live_test_passed is not True
            or review.live_test_at is None
            or not now - REVIEW_MAX_AGE <= review.live_test_at <= evidence.reviewed_at
            or not review.live_test_reference
            or review.zero_charge_verified is not True
            or not review.zero_charge_reference
        ):
            return False
    return True
