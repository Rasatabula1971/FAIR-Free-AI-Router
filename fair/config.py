import os
from pathlib import Path

import yaml
from pydantic import Field

from fair.benchmarks.contracts import BenchmarkPolicy
from fair.quality.contracts import SourcePolicy
from fair.router.scheduler import SchedulerSettings
from fair.schemas.domain import DTO


class RoutingSettings(DTO):
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
    quality_weight: float = Field(default=0.65, ge=0)
    quota_weight: float = Field(default=0.20, ge=0)
    reliability_weight: float = Field(default=0.15, ge=0)


def config_dir() -> Path:
    return Path(os.environ.get("FAIR_CONFIG_DIR", "config"))


def load_yaml(name: str):
    with (config_dir() / name).open(encoding="utf-8") as file:
        return yaml.safe_load(file)
