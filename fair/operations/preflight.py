"""Validate deployment inputs without inference, migrations, or database writes."""

import argparse
import json
import os
from pathlib import Path

import yaml

from fair.benchmarks.registry import BenchmarkRegistry
from fair.config import RoutingSettings
from fair.operations.status import schema_current
from fair.providers.live import LiveSettings, register_live
from fair.providers.registry import Registry
from fair.quality.source_reviews import SourceReviewRegistry
from fair.quality.thresholds import validate_thresholds
from fair.schemas.db import database
from fair.schemas.domain import ProviderSpec
from fair.security.credentials import APIKeys


def check(directory, *, check_database=False, allow_demo=False):
    checks = {}
    try:
        checks["api_credentials"] = APIKeys.load().configured
    except Exception:
        checks["api_credentials"] = False
    checks["execution_mode"] = os.environ.get("FAIR_DEMO_MODE") != "1" or allow_demo
    try:
        directory = Path(directory)

        def read(name):
            return yaml.safe_load((directory / name).read_text(encoding="utf-8"))

        RoutingSettings.model_validate(read("routing.yaml"))
        validate_thresholds(read("quality_thresholds.yaml"))
        reviews = Path(
            os.environ.get("FAIR_SOURCE_REVIEWS_FILE", directory / "source_reviews.yaml")
        )
        if reviews.exists() or os.environ.get("FAIR_SOURCE_REVIEWS_FILE"):
            SourceReviewRegistry.from_file(reviews)
        if os.environ.get("FAIR_BENCHMARKS_FILE"):
            BenchmarkRegistry.from_file(os.environ["FAIR_BENCHMARKS_FILE"])
        live = (
            LiveSettings.model_validate(read("live_adapters.yaml"))
            if (directory / "live_adapters.yaml").exists()
            else LiveSettings()
        )
        specs = [ProviderSpec.model_validate(item) for item in read("providers.yaml")["providers"]]
        registry = Registry()
        register_live(registry, specs, live)
        if live.enabled and os.environ.get("FAIR_DEMO_MODE") == "1":
            raise ValueError()
        checks["routing_and_provider_configuration"] = True
    except Exception:
        checks["routing_and_provider_configuration"] = False
    if check_database:
        engine = None
        try:
            url = os.environ["FAIR_DATABASE_URL"]
            # A check must not create a missing SQLite database as a side effect.
            from sqlalchemy.engine import make_url

            parsed = make_url(url)
            if parsed.get_backend_name() == "sqlite" and (
                not parsed.database
                or parsed.database == ":memory:"
                or not Path(parsed.database).is_file()
            ):
                raise ValueError()
            if parsed.get_backend_name() == "sqlite":
                url = parsed.set(
                    database="file:" + Path(parsed.database).resolve().as_posix(),
                    query={"mode": "ro", "uri": "true"},
                ).render_as_string(hide_password=False)
            engine, sessions = database(url)
            with sessions() as session:
                checks["database_schema"] = schema_current(session)
        except Exception:
            checks["database_schema"] = False
        finally:
            if engine is not None:
                engine.dispose()
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "database_checked": check_database,
        "checks": checks,
        "provider_calls": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-directory", type=Path, default=Path(os.environ.get("FAIR_CONFIG_DIR", "config"))
    )
    parser.add_argument("--check-database", action="store_true")
    parser.add_argument("--allow-demo", action="store_true")
    args = parser.parse_args()
    result = check(
        args.config_directory, check_database=args.check_database, allow_demo=args.allow_demo
    )
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
