from pydantic import Field

from fair.quality.contracts import SourcePolicy
from fair.schemas.domain import DTO


class RoutingSettings(DTO):
    cache_enabled: bool = False
    cache_ttl_seconds: int = Field(default=3600, ge=1, le=86400)
    cache_max_entries: int = Field(default=1000, ge=1, le=100000)
    confidence_half_life_days: int = Field(default=30, ge=1, le=365)
    drift_drop_points: float = Field(default=15, gt=0, le=100, allow_inf_nan=False)
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_verification_attempts: int = Field(default=2, ge=1, le=3)
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    circuit_failures: int = Field(default=3, ge=1)
    circuit_window_seconds: float = Field(default=60, gt=0)
    cooldown_seconds: float = Field(default=60, gt=0)
    quality_weight: float = Field(default=0.65, ge=0)
    quota_weight: float = Field(default=0.20, ge=0)
    reliability_weight: float = Field(default=0.15, ge=0)
    source_policy: SourcePolicy | None = None
