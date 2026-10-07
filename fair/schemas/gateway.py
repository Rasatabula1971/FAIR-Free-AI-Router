"""Operator review of the models a gateway may serve; never store credentials here."""

import json
import re
from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

# What FreeLLMAPI reports in X-Routed-Via for a real upstream: a platform, a slash,
# and that platform's own model id. "cache" and "idempotency" carry no slash, so a
# replayed answer can never be listed as a reviewed route.
_ROUTE = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[\x21-\x7e]{1,191}$"


class GatewayEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GatewayModelReview(GatewayEvidence):
    # The id FAIR sends: a concrete entry from the gateway's own /v1/models.
    model_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    # The smallest window among the reviewed routes, not the catalog's largest.
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    # Off until a schema request has been seen to work on every reviewed route.
    structured_output: StrictBool = False
    # Shared with any other descriptor that names the same weights, so a cross-check
    # cannot be satisfied by asking one model through two doors.
    independence_group: str | None = Field(default=None, min_length=1, max_length=128)
    # Every X-Routed-Via value this model may be answered from. Each one is a provider
    # account the operator has checked is on a plan that cannot bill.
    routes: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def reviewed_routes(self):
        if len(set(self.routes)) != len(self.routes):
            raise ValueError("Duplicate route")
        if not all(re.fullmatch(_ROUTE, route) for route in self.routes):
            raise ValueError("A route is '<platform>/<upstream model id>'")
        return self


class GatewayReview(GatewayEvidence):
    # The day every route below was checked. Evidence built from it expires on the
    # same 30-day clock as a built-in provider review, so restarting renews nothing.
    reviewed_at: date
    reviewer_reference: str = Field(min_length=1, max_length=128, pattern=r"\S")
    models: list[GatewayModelReview] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_models(self):
        if len({model.model_id for model in self.models}) != len(self.models):
            raise ValueError("Duplicate model review")
        return self

    @property
    def reviewed(self) -> datetime:
        return datetime(
            self.reviewed_at.year, self.reviewed_at.month, self.reviewed_at.day, tzinfo=UTC
        )


def load_gateway_review(source) -> GatewayReview:
    """A review from a mapping, or from the path of a JSON file holding one."""
    if isinstance(source, GatewayReview):
        return source
    if isinstance(source, str):
        with open(source, encoding="utf-8") as handle:
            source = json.load(handle)
    return GatewayReview.model_validate(source)
