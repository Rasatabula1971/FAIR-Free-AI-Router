"""Run one reviewed provider through FAIR without changing production routing state."""

import argparse
import asyncio
import json
from pathlib import Path

import yaml

from fair.config import RoutingSettings
from fair.providers.live import LiveSettings, register_live
from fair.providers.registry import Registry
from fair.router.orchestrator import Router
from fair.schemas.api import SolveRequest
from fair.schemas.db import Base, database
from fair.schemas.domain import ProviderSpec


async def smoke(directory, provider_id, model_id):
    directory = Path(directory)
    settings = LiveSettings.model_validate(
        yaml.safe_load((directory / "live_adapters.yaml").read_text(encoding="utf-8"))
    )
    entries = yaml.safe_load((directory / "providers.yaml").read_text(encoding="utf-8"))[
        "providers"
    ]
    matches = [
        ProviderSpec.model_validate(item)
        for item in entries
        if item.get("provider_id") == provider_id
    ]
    if len(matches) != 1 or not settings.enabled:
        raise ValueError("An enabled, reviewed provider configuration is required")
    spec = matches[0]
    models = [item for item in spec.models if item.model_id == model_id and item.active]
    if len(models) != 1:
        raise ValueError("An exact reviewed model is required")
    spec.models = models
    registry = Registry()
    register_live(registry, [spec], settings)
    if provider_id not in registry.adapters:
        raise ValueError("Provider is not active")
    engine, sessions = database("sqlite:///:memory:")
    router = None
    try:
        Base.metadata.create_all(engine)
        router = Router(
            registry,
            RoutingSettings(max_attempts=1, timeout_seconds=60),
            {"commodity": 75, "standard": 82, "advanced": 88, "high_impact_support": 92},
            sessions,
        )
        result = await router.solve(
            SolveRequest(
                client_id="provider-smoke",
                task="Compute 2 + 2. Return only the number, without explanation.",
                validation={"kind": "arithmetic", "expression": "2 + 2"},
            )
        )
        return {
            "provider_id": provider_id,
            "model_id": model_id,
            "status": result.status,
            "verification_state": result.verification_state,
            "attempts": len(result.attempts),
            "paid_inference_executed": result.paid_inference_executed,
            "reason_code": result.reason_code,
        }
    finally:
        if router is not None:
            await router.close()
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-directory", type=Path, required=True)
    parser.add_argument(
        "--provider", choices=["groq", "openrouter_free", "ollama_local"], required=True
    )
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(smoke(args.config_directory, args.provider, args.model))
    except Exception:
        # Provider/configuration exceptions may include secrets; do not print their text.
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason_code": "PROVIDER_SMOKE_FAILED",
                    "paid_inference_executed": None,
                }
            )
        )
        return 1
    print(json.dumps(result))
    return 0 if result["status"] == "ACCEPTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
