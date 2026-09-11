"""Operator-reviewed evidence; never store credentials or raw provider responses here."""

from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, model_validator


class FreeStatus(StrEnum):
    VERIFIED_FREE_PLAN = "verified_free_plan"
    VERIFIED_ZERO_PRICE_MODEL = "verified_zero_price_model"
    LOCAL_COMPUTE = "local_compute"
    DEVELOPMENT_ONLY = "development_only_free"
    CONFLICTING = "conflicting"
    TRIAL_CREDIT = "trial_credit"
    PROMOTIONAL_CREDIT = "promotional_credit"
    CREDIT_BASED = "credit_based"
    PAID = "paid"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ModelQualification(Evidence):
    model_id: str = Field(min_length=1, max_length=256)
    model_revision: str | None = Field(default=None, min_length=1, max_length=128)
    input_price_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_price_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    request_price: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    pricing_reference: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"\S")
    paid_tools_enabled: StrictBool | None = None
    live_test_passed: StrictBool = False
    live_test_at: AwareDatetime | None = None
    live_test_reference: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"\S"
    )
    zero_charge_verified: StrictBool = False
    zero_charge_reference: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"\S"
    )


class ProviderQualification(Evidence):
    provider_id: str = Field(min_length=1, max_length=128)
    free_status: FreeStatus = FreeStatus.UNKNOWN
    production_allowed: StrictBool = False
    billing_enabled: StrictBool | None = None
    payment_method_required: StrictBool | None = None
    payment_method_present: StrictBool | None = None
    can_auto_bill: StrictBool | None = None
    reviewed_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    reviewer_reference: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"\S"
    )
    billing_reference: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"\S")
    terms_reference: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"\S")
    privacy_reference: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"\S")
    limits_reference: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"\S")
    models: list[ModelQualification] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_models(self):
        if len({m.model_id for m in self.models}) != len(self.models):
            raise ValueError("Duplicate model qualification")
        return self
