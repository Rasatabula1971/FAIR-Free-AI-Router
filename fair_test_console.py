"""Interactive Windows-friendly smoke tests for FAIR.

This console never prints API keys. Recurring/free-plan cloud providers are
enabled only after an explicit per-session operator confirmation.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from fair.config import RoutingSettings
from fair.embedded import FAIR
from fair.embedded.router import EmbeddedRouter
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quality.thresholds import DEFAULT_THRESHOLDS
from fair.schemas.api import SolveRequest
from fair.schemas.domain import ProviderSpec

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

CONFIRMABLE_PROVIDERS = {
    "1": ("google_gemini_api", "Google Gemini", "GEMINI_API_KEY"),
    "2": ("groq", "Groq", "GROQ_API_KEY"),
    "3": ("mistral", "Mistral", "MISTRAL_API_KEY"),
    "4": ("zai_free", "Z.ai", "ZAI_API_KEY"),
    "5": ("cloudflare_workers_ai", "Cloudflare Workers AI", "CLOUDFLARE_API_TOKEN"),
}

_session_confirmed: set[str] | None = None


def _dotenv_values() -> dict[str, str]:
    """Read enough dotenv syntax to detect configured providers; never display values."""
    if not ENV_FILE.exists():
        return {}
    values: dict[str, str] = {}
    try:
        for raw_line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
    except (OSError, UnicodeError):
        return {}
    return values


def _configured_env_names() -> set[str]:
    file_values = _dotenv_values()
    names = {name for name, value in file_values.items() if value.strip()}
    names.update(name for name, value in os.environ.items() if value.strip())
    return names


def _confirm_free_accounts() -> set[str]:
    """Get explicit free-only account attestations once for this console session."""
    global _session_confirmed
    if _session_confirmed is not None:
        return set(_session_confirmed)

    configured = _configured_env_names()
    choices = {
        number: details
        for number, details in CONFIRMABLE_PROVIDERS.items()
        if details[2] in configured
    }
    if not choices:
        _session_confirmed = set()
        return set()

    print()
    print("Recurring/free-plan providers detected")
    print("--------------------------------------")
    for number, (_, label, env_name) in choices.items():
        print(f"  {number}. {label} ({env_name})")
    print()
    print("FAIR will not use these merely because an API key exists.")
    print("Only confirm an account if you have verified that its current")
    print("configuration cannot auto-bill or otherwise incur paid API usage.")
    print("OpenRouter Free and Kilo Free do not need this confirmation because")
    print("FAIR checks their zero-price/free status at runtime.")
    print()
    answer = input(
        "Confirm for THIS SESSION (example: 1,2), A=all shown, Enter=none: "
    ).strip()

    if not answer:
        _session_confirmed = set()
        return set()
    if answer.casefold() == "a":
        _session_confirmed = {details[0] for details in choices.values()}
        return set(_session_confirmed)

    selected: set[str] = set()
    for item in answer.split(","):
        number = item.strip()
        if number in choices:
            selected.add(choices[number][0])
        elif number:
            print(f"Ignoring unknown selection: {number}")
    _session_confirmed = selected
    return set(selected)


def _provider_spec(provider_id: str, model_id: str) -> ProviderSpec:
    return ProviderSpec(
        provider_id=provider_id,
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        models=[
            {
                "model_id": model_id,
                "context_window": 32768,
                "capabilities": {"reasoning", "coding", "structured_output"},
            }
        ],
    )


def _offline_router(
    entries: list[tuple[ProviderSpec, MockAdapter]],
    *,
    max_attempts: int = 3,
    max_unanswered_attempts: int = 6,
) -> EmbeddedRouter:
    registry = Registry()
    for spec, adapter in entries:
        registry.register(spec, adapter)
    settings = RoutingSettings(
        max_attempts=max_attempts,
        max_unanswered_attempts=max_unanswered_attempts,
        cache_enabled=False,
    )
    return EmbeddedRouter(registry, settings, dict(DEFAULT_THRESHOLDS))


def _arithmetic_request(*, cross_check: bool = False) -> SolveRequest:
    return SolveRequest(
        client_id="fair-test-console",
        task="What is 15 multiplied by 23?",
        quality_level="standard",
        validation={"kind": "arithmetic", "expression": "15*23"},
        cross_check_required=cross_check,
    )


def _live_fair() -> FAIR:
    kwargs: dict[str, object] = {
        "confirmed_free_providers": _confirm_free_accounts(),
        "cache_enabled": False,
    }
    if ENV_FILE.exists():
        kwargs["env_file"] = str(ENV_FILE)
    return FAIR(**kwargs)


def _print_result(result) -> None:
    print()
    print("Result")
    print("------")
    print(f"Status:       {result.status}")
    print(f"Reason:       {result.reason_code}")
    print(f"Provider:     {result.provider_id or '-'}")
    print(f"Model:        {result.model_id or '-'}")
    print(f"Verification: {result.verification_state or '-'}")
    print(f"Output:       {result.output if result.output is not None else '-'}")
    if result.cross_check is not None and result.cross_check.required:
        print(f"Cross-check:  {result.cross_check.state}")
    if result.attempts:
        print()
        print("Attempts:")
        for attempt in result.attempts:
            print(
                "  "
                f"#{attempt.attempt_number} "
                f"{attempt.provider_id}/{attempt.model_id} "
                f"{attempt.role}: {attempt.disposition}"
            )


def _print_live_inventory(fair: FAIR) -> None:
    providers = fair.providers()
    print()
    print("Eligible providers")
    print("------------------")
    if not providers:
        print("  None")
    for provider in providers:
        print(
            f"  {provider['provider_id']} "
            f"[{provider['access_class']}] "
            f"status={provider['status']}"
        )
        for model in provider["models"]:
            print(f"      - {model}")

    if fair.skipped:
        print()
        print("Skipped providers")
        print("-----------------")
        for provider_id, reason in sorted(fair.skipped.items()):
            print(f"  {provider_id}: {reason}")


async def quick_offline_test() -> bool:
    print()
    print("Running deterministic offline checks...")

    good_router = _offline_router(
        [(_provider_spec("offline-good", "mock-good"), MockAdapter("offline-good", text="345"))]
    )
    good = await good_router.solve(_arithmetic_request())

    bad_router = _offline_router(
        [(_provider_spec("offline-bad", "mock-bad"), MockAdapter("offline-bad", text="999"))]
    )
    bad = await bad_router.solve(_arithmetic_request())

    accepted_ok = (
        good.status == "ACCEPTED"
        and good.output == "345"
        and good.verification_state == "DETERMINISTIC_ARITHMETIC"
    )
    rejected_ok = bad.status == "ESCALATION_REQUIRED" and bad.output is None

    print(f"  Correct answer accepted: {'PASS' if accepted_ok else 'FAIL'}")
    print(f"  Wrong answer rejected:   {'PASS' if rejected_ok else 'FAIL'}")
    return accepted_ok and rejected_ok


async def live_provider_test() -> bool:
    print()
    print("Running one real arithmetic request through FAIR...")
    try:
        async with _live_fair() as fair:
            _print_live_inventory(fair)
            result = await fair.solve(
                "What is 15 multiplied by 23?",
                validation={"kind": "arithmetic", "expression": "15*23"},
                cache_mode="bypass",
            )
            _print_result(result)
            return result.status == "ACCEPTED" and result.output == "345"
    except Exception as error:
        print()
        print(f"Live test stopped safely: {type(error).__name__}")
        print("No API key or upstream exception text was printed.")
        return False


async def failover_test() -> bool:
    print()
    print("Running deterministic provider failover test...")
    first = _provider_spec("a-down", "mock-down")
    second = _provider_spec("b-healthy", "mock-healthy")
    router = _offline_router(
        [
            (first, MockAdapter("a-down", error=TimeoutError())),
            (second, MockAdapter("b-healthy", text="345")),
        ],
        max_attempts=1,
    )
    result = await router.solve(_arithmetic_request())
    _print_result(result)
    passed = (
        result.status == "ACCEPTED"
        and result.provider_id == "b-healthy"
        and len(result.attempts) == 2
        and result.attempts[0].disposition == "INFRA_FAILURE"
    )
    print()
    print(f"Failover: {'PASS' if passed else 'FAIL'}")
    return passed


async def live_cross_check_test() -> bool:
    print()
    print("Running a live independent cross-check...")
    try:
        async with _live_fair() as fair:
            _print_live_inventory(fair)
            provider_ids = {provider["provider_id"] for provider in fair.providers()}
            if len(provider_ids) < 2:
                print()
                print("Cross-check needs at least two eligible independent providers.")
                print("No live request was sent.")
                return False
            result = await fair.solve(
                "What is 15 multiplied by 23?",
                validation={"kind": "arithmetic", "expression": "15*23"},
                cross_check_required=True,
                cache_mode="bypass",
            )
            _print_result(result)
            return result.status == "ACCEPTED" and result.cross_check.state == "PASSED"
    except Exception as error:
        print()
        print(f"Cross-check stopped safely: {type(error).__name__}")
        print("No API key or upstream exception text was printed.")
        return False


def full_test_suite() -> bool:
    print()
    print("Running the complete pytest suite...")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-ra"],
        cwd=ROOT,
        check=False,
    )
    print()
    print(f"Full suite: {'PASS' if completed.returncode == 0 else 'FAIL'}")
    return completed.returncode == 0


async def show_live_providers() -> bool:
    try:
        async with _live_fair() as fair:
            _print_live_inventory(fair)
            return bool(fair.providers())
    except Exception as error:
        print()
        print(f"Provider inventory stopped safely: {type(error).__name__}")
        print("No API key or upstream exception text was printed.")
        return False


def _pause() -> None:
    input("\nPress Enter to return to the menu...")


def _header() -> None:
    print()
    print("=" * 46)
    print("              FAIR TEST CONSOLE")
    print("=" * 46)
    print(f".env: {'FOUND' if ENV_FILE.exists() else 'NOT FOUND'}")
    print()
    print("1. Quick offline validation test")
    print("2. Test a live provider")
    print("3. Test routing / failover (offline)")
    print("4. Test live independent cross-check")
    print("5. Run full pytest suite")
    print("6. Show eligible / skipped providers")
    print("7. Exit")
    print()


def main() -> int:
    while True:
        _header()
        choice = input("Choose 1-7: ").strip()
        try:
            if choice == "1":
                asyncio.run(quick_offline_test())
                _pause()
            elif choice == "2":
                asyncio.run(live_provider_test())
                _pause()
            elif choice == "3":
                asyncio.run(failover_test())
                _pause()
            elif choice == "4":
                asyncio.run(live_cross_check_test())
                _pause()
            elif choice == "5":
                full_test_suite()
                _pause()
            elif choice == "6":
                asyncio.run(show_live_providers())
                _pause()
            elif choice == "7":
                print("Exiting FAIR Test Console.")
                return 0
            else:
                print("Please choose a number from 1 to 7.")
        except KeyboardInterrupt:
            print("\nCancelled. Returning to menu.")


if __name__ == "__main__":
    raise SystemExit(main())
