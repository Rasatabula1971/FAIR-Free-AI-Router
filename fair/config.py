import os
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from fair.benchmarks.contracts import BenchmarkPolicy
from fair.constants import SECONDS_IN_DAY
from fair.quality.contracts import SourcePolicy
from fair.router.scheduler import SchedulerSettings
from fair.schemas.domain import DTO


class RoutingSettings(DTO):
    cache_enabled: bool = False
    cache_ttl_seconds: int = Field(default=3600, ge=1, le=SECONDS_IN_DAY)
    cache_max_entries: int = Field(default=1000, ge=1, le=100000)
    feedback_weight: float = Field(default=3, ge=0, le=5, allow_inf_nan=False)
    feedback_max_age_days: int = Field(default=30, ge=1, le=365)
    confidence_half_life_days: int = Field(default=30, ge=1, le=365)
    drift_drop_points: float = Field(default=15, gt=0, le=100, allow_inf_nan=False)
    shadow_enabled: bool = False
    shadow_min_headroom: float = Field(default=0.7, ge=0.4, le=0.95, allow_inf_nan=False)
    shadow_max_rate: float = Field(default=0.1, gt=0, le=0.1, allow_inf_nan=False)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    benchmark_policy: BenchmarkPolicy | None = None
    source_policy: SourcePolicy | None = None
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_verification_attempts: int = Field(default=2, ge=1, le=3)
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    circuit_failures: int = Field(default=3, ge=1)
    circuit_window_seconds: float = Field(default=60, gt=0)
    cooldown_seconds: float = Field(default=60, gt=0)
    quality_weight: float = Field(default=0.65, ge=0, le=1, allow_inf_nan=False)
    quota_weight: float = Field(default=0.20, ge=0, le=1, allow_inf_nan=False)
    reliability_weight: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def _selector_weights_sum_to_one(self):
        total = self.quality_weight + self.quota_weight + self.reliability_weight
        if not (0.99 <= total <= 1.01):
            raise ValueError(
                f"Selector weights must sum to 1.0 (got {total:.4f})"
            )
        return self


def config_dir() -> Path:
    return Path(os.environ.get("FAIR_CONFIG_DIR", "config"))


def load_yaml(name: str):
    with (config_dir() / name).open(encoding="utf-8") as file:
        return yaml.safe_load(file)
