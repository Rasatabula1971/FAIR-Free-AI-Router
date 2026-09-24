"""Interactive Windows-friendly smoke tests for FAIR.

The console never prints API keys or raw provider responses. Live provider tests
can isolate one provider at a time, and recurring/free-plan providers require an
explicit per-session operator confirmation before FAIR enables them.
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
from fair.providers.base import BillingViolation
from fair.providers.mock import MockAdapter
from fair.providers.registry import Registry
from fair.quality.thresholds import DEFAULT_THRESHOLDS
from fair.schemas.api import SolveRequest
from fair.schemas.domain import ProviderSpec

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

LIVE_PROVIDERS = {
    "1": {
        "provider_id": "google_gemini_api",
        "label": "Google Gemini",
        "env": "GEMINI_API_KEY",
        "kwarg": "gemini_api_key",
        "confirmation_required": True,
    },
    "2": {
        "provider_id": "openrouter_free",
        "label": "OpenRouter Free",
        "env": "OPENROUTER_API_KEY",
        "kwarg": "openrouter_api_key",
        "confirmation_required": False,
    },
    "3": {
        "provider_id": "kilo_free",
        "label": "Kilo Free",
        "env": "KILO_API_KEY",
        "kwarg": "kilo_api_key",
        "confirmation_required": False,
    },
    "4": {
        "provider_id": "groq",
        "label": "Groq",
        "env": "GROQ_API_KEY",
        "kwarg": "groq_api_key",
        "confirmation_required": True,
    },
    "5": {
        "provider_id": "mistral",
        "label": "Mistral",
        "env": "MISTRAL_API_KEY",
        "kwarg": "mistral_api_key",
        "confirmation_required": True,
    },
    "6": {
        "provider_id": "zai_free",
        "label": "Z.ai",
        "env": "ZAI_API_KEY",
        "kwarg": "zai_api_key",
        "confirmation_required": True,
    },
    "7": {
        "provider_id": "cloudflare_workers_ai",
        "label": "Cloudflare Workers AI",
        "env": "CLOUDFLARE_API_TOKEN",
        "kwarg": "cloudflare_api_token",
        "confirmation_required": True,
    },
    "8": {
        "provider_id": "ollama_local",
        "label": "Ollama Local",
        "env": None,
        "kwarg": None,
        "confirmation_required": False,
    },
}

_session_confirmed: set[str] = set()


def _dotenv_values() -> dict[str, str]:
    """Read enough dotenv syntax for local test configuration; never print values."""
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


def _env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    value = _dotenv_values().get(name)
    return value.strip() if value is not None and value.strip() else None


def _provider_status(entry: dict[str, object]) -> tuple[bool, str]:
    provider_id = str(entry["provider_id"])
    if provider_id == "ollama_local":
        url = _env_value("OLLAMA_URL") or _env_value("OLLAMA_HOST")
        enabled = _env_value("OLLAMA_ENABLED")
        if url or enabled:
            return True, "configured"
        return False, "OLLAMA_URL/OLLAMA_HOST not configured"

    env_name = str(entry["env"])
    if not _env_value(env_name):
        return False, f"{env_name} missing"
    if provider_id == "cloudflare_workers_ai" and not _env_value("CLOUDFLARE_ACCOUNT_ID"):
        return False, "CLOUDFLARE_ACCOUNT_ID missing"
    return True, "configured"


def _configured_provider_ids() -> list[str]:
    return [
        str(entry["provider_id"])
        for entry in LIVE_PROVIDERS.values()
        if _provider_status(entry)[0]
    ]


def _entry_for_provider(provider_id: str) -> dict[str, object]:
    for entry in LIVE_PROVIDERS.values():
        if entry["provider_id"] == provider_id:
            return entry
    raise ValueError("Unknown provider")


def _confirm_provider(entry: dict[str, object]) -> bool:
    provider_id = str(entry["provider_id"])
    if not bool(entry["confirmation_required"]):
        return True
    if provider_id in _session_confirmed:
        return True

    label = str(entry["label"])
    print()
    print(f"{label} requires explicit free-only account confirmation.")
    print("Only answer Y if you have verified that this account/configuration")
    print("cannot auto-bill or otherwise incur paid API usage.")
    answer = input(f"Confirm {label} for THIS SESSION? [y/N]: ").strip().casefold()
    if answer in {"y", "yes"}:
        _session_confirmed.add(provider_id)
        return True
    return False


def _confirm_free_accounts() -> set[str]:
    """Offer confirmation for each configured provider that still needs it."""
    for entry in LIVE_PROVIDERS.values():
        configured, _ = _provider_status(entry)
        provider_id = str(entry["provider_id"])
        if (
            configured
            and bool(entry["confirmation_required"])
            and provider_id not in _session_confirmed
        ):
            _confirm_provider(entry)
    return set(_session_confirmed)


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


def _selected_live_fair(provider_ids: set[str]) -> FAIR:
    """Construct FAIR with only the selected providers, never every key in .env."""
    kwargs: dict[str, object] = {
        "cache_enabled": False,
        "confirmed_free_providers": set(),
    }
    confirmed: set[str] = set()

    for provider_id in provider_ids:
        entry = _entry_for_provider(provider_id)
        configured, reason = _provider_status(entry)
        if not configured:
            raise ValueError(f"{entry['label']}: {reason}")
        if not _confirm_provider(entry):
            raise PermissionError(f"{entry['label']} was not confirmed for free-only use")

        if bool(entry["confirmation_required"]):
            confirmed.add(provider_id)

        if provider_id == "ollama_local":
            url = _env_value("OLLAMA_URL") or _env_value("OLLAMA_HOST")
            if not url and _env_value("OLLAMA_ENABLED"):
                url = "http://127.0.0.1:11434"
            if not url:
                raise ValueError("Ollama local endpoint unavailable")
            kwargs["ollama_url"] = url
            continue

        env_name = str(entry["env"])
        kwarg = str(entry["kwarg"])
        value = _env_value(env_name)
        if not value:
            raise ValueError(f"{env_name} missing")
        kwargs[kwarg] = value

        if provider_id == "cloudflare_workers_ai":
            account_id = _env_value("CLOUDFLARE_ACCOUNT_ID")
            if not account_id:
                raise ValueError("CLOUDFLARE_ACCOUNT_ID missing")
            kwargs["cloudflare_account_id"] = account_id

    kwargs["confirmed_free_providers"] = confirmed
    return FAIR(**kwargs)


def _all_live_fair() -> FAIR:
    kwargs: dict[str, object] = {
        "confirmed_free_providers": _confirm_free_accounts(),
        "cache_enabled": False,
    }
    if ENV_FILE.exists():
        kwargs["env_file"] = str(ENV_FILE)
    return FAIR(**kwargs)


def _safe_provider_diagnostics(fair: FAIR, provider_id: str) -> dict:
    """Read fixed, secret-scanned diagnostics from a registered adapter."""
    adapter = fair._registry.adapters.get(provider_id)
    method = getattr(adapter, "safe_diagnostics", None)
    if not callable(method):
        return {}
    result = method()
    return result if isinstance(result, dict) else {}


def _print_kilo_diagnostic(fair: FAIR) -> None:
    state = _safe_provider_diagnostics(fair, "kilo_free").get(
        "cost_microdollars", "NOT_AVAILABLE"
    )
    descriptions = {
        "ZERO": "present and verified zero",
        "COST_FIELD_MISSING": "usage present, cost_microdollars missing",
        "USAGE_MISSING": "usage object missing",
        "NONZERO_OR_INVALID": "present but non-zero or invalid",
        "NOT_OBSERVED": "no completion cost observation was reached",
        "NOT_AVAILABLE": "diagnostic unavailable",
    }
    print()
    print("Kilo zero-cost diagnostic")
    print("-------------------------")
    print(f"Cost observation: {state}")
    print(f"Meaning:          {descriptions.get(str(state), 'unknown state')}")
    if state == "ZERO":
        print("Safety result:    ZERO COST VERIFIED")
    else:
        print("Safety result:    FAIL-CLOSED - zero cost was not proven")


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


def _show_provider_configuration() -> None:
    print()
    print("Provider configuration")
    print("----------------------")
    for number, entry in LIVE_PROVIDERS.items():
        configured, reason = _provider_status(entry)
        provider_id = str(entry["provider_id"])
        if bool(entry["confirmation_required"]):
            confirmation = (
                "confirmed this session"
                if provider_id in _session_confirmed
                else "confirmation required"
            )
        else:
            confirmation = "runtime/local safety check"
        state = "CONFIGURED" if configured else "NOT CONFIGURED"
        print(
            f"  {number}. {entry['label']}: {state} "
            f"({reason}; {confirmation})"
        )


def _choose_provider_ids(*, exact_count: int) -> set[str] | None:
    _show_provider_configuration()
    print()
    if exact_count == 1:
        answer = input("Choose provider number, or Enter to cancel: ").strip()
        numbers = [answer] if answer else []
    else:
        answer = input(
            f"Choose exactly {exact_count} provider numbers separated by commas, "
            "or Enter to cancel: "
        ).strip()
        numbers = [item.strip() for item in answer.split(",") if item.strip()]

    if not numbers:
        return None
    if len(numbers) != exact_count or len(set(numbers)) != exact_count:
        print(f"Please select exactly {exact_count} different provider(s).")
        return None
    if any(number not in LIVE_PROVIDERS for number in numbers):
        print("One or more provider numbers are invalid.")
        return None

    selected = {str(LIVE_PROVIDERS[number]["provider_id"]) for number in numbers}
    for provider_id in selected:
        entry = _entry_for_provider(provider_id)
        configured, reason = _provider_status(entry)
        if not configured:
            print(f"{entry['label']} cannot run: {reason}")
            return None
    return selected


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


async def _run_selected_live_provider(provider_id: str) -> bool:
    entry = _entry_for_provider(provider_id)
    print()
    print(f"Testing only: {entry['label']}")
    try:
        async with _selected_live_fair({provider_id}) as fair:
            _print_live_inventory(fair)
            try:
                result = await fair.solve(
                    "What is 15 multiplied by 23?",
                    validation={"kind": "arithmetic", "expression": "15*23"},
                    cache_mode="bypass",
                )
            except BillingViolation:
                print()
                print("FAIR billing protection: TRIGGERED")
                print("The provider was stopped because zero-cost use was not proven.")
                if provider_id == "kilo_free":
                    _print_kilo_diagnostic(fair)
                return False

            _print_result(result)
            if provider_id == "kilo_free":
                _print_kilo_diagnostic(fair)
            passed = result.status == "ACCEPTED" and result.output == "345"
            print()
            print(f"Provider test: {'PASS' if passed else 'FAIL'}")
            return passed
    except Exception as error:
        print()
        print(f"Provider test stopped safely: {type(error).__name__}")
        print("No API key or upstream exception text was printed.")
        return False


async def live_provider_test() -> bool:
    selected = _choose_provider_ids(exact_count=1)
    if not selected:
        return False
    return await _run_selected_live_provider(next(iter(selected)))


async def live_provider_sweep() -> bool:
    print()
    print("Testing FAIR provider matrix one provider at a time.")
    print("Configured providers are isolated; one failure does not stop the sweep.")
    print("Unconfigured providers are reported as SKIP instead of disappearing.")

    results: list[tuple[str, str, str]] = []
    attempted = 0
    passed_count = 0

    for entry in LIVE_PROVIDERS.values():
        provider_id = str(entry["provider_id"])
        label = str(entry["label"])
        configured, reason = _provider_status(entry)

        if not configured:
            results.append((label, "SKIP", reason))
            continue

        attempted += 1
        passed = await _run_selected_live_provider(provider_id)
        if passed:
            passed_count += 1
            results.append((label, "PASS", "validated answer and FAIR safety checks"))
        else:
            results.append((label, "FAIL-CLOSED", "provider failed or zero-cost use was not proven"))

    print()
    print("Provider sweep summary")
    print("----------------------")
    for label, state, reason in results:
        print(f"  {label:<24} {state:<11} {reason}")

    print()
    print(f"Configured providers attempted: {attempted}")
    print(f"Passed:                       {passed_count}")
    print(f"Failed / fail-closed:         {attempted - passed_count}")
    print(f"Not configured:               {len(results) - attempted}")

    if attempted == 0:
        print()
        print("No live providers are configured.")
        return False
    return passed_count == attempted


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
    print("Choose the two providers that FAIR may use for this cross-check.")
    selected = _choose_provider_ids(exact_count=2)
    if not selected:
        return False

    labels = [_entry_for_provider(provider_id)["label"] for provider_id in sorted(selected)]
    print()
    print("Cross-check pool: " + " + ".join(str(label) for label in labels))
    try:
        async with _selected_live_fair(selected) as fair:
            _print_live_inventory(fair)
            try:
                result = await fair.solve(
                    "What is 15 multiplied by 23?",
                    validation={"kind": "arithmetic", "expression": "15*23"},
                    cross_check_required=True,
                    cache_mode="bypass",
                )
            except BillingViolation:
                print()
                print("FAIR billing protection: TRIGGERED")
                if "kilo_free" in selected:
                    _print_kilo_diagnostic(fair)
                return False

            _print_result(result)
            passed = result.status == "ACCEPTED" and result.cross_check.state == "PASSED"
            print()
            print(f"Cross-check test: {'PASS' if passed else 'FAIL'}")
            return passed
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
    _show_provider_configuration()
    print()
    print("To calculate FAIR eligibility, recurring/free-plan accounts may need")
    print("confirmation for this session.")
    try:
        async with _all_live_fair() as fair:
            _print_live_inventory(fair)
            return bool(fair.providers())
    except Exception as error:
        print()
        print(f"Provider inventory stopped safely: {type(error).__name__}")
        print("No API key or upstream exception text was printed.")
        return False


def reset_confirmations() -> None:
    _session_confirmed.clear()
    print("Session confirmations cleared.")


def _pause() -> None:
    input("\nPress Enter to return to the menu...")


def _header() -> None:
    print()
    print("=" * 50)
    print("                FAIR TEST CONSOLE")
    print("=" * 50)
    print(f".env: {'FOUND' if ENV_FILE.exists() else 'NOT FOUND'}")
    print()
    print("1. Quick offline validation test")
    print("2. Test ONE live provider")
    print("3. Test ALL configured live providers")
    print("4. Test routing / failover (offline)")
    print("5. Test live cross-check (choose TWO providers)")
    print("6. Run full pytest suite")
    print("7. Show provider configuration / eligibility")
    print("8. Clear this-session free-account confirmations")
    print("9. Exit")
    print()


def _command_line_mode() -> int | None:
    """Run one-shot modes used by START_FAIR.bat and automation."""
    if len(sys.argv) < 2:
        return None

    command = sys.argv[1].strip().casefold()

    if command in {"--menu", "menu"}:
        return None

    if command in {"--sweep", "sweep"}:
        return 0 if asyncio.run(live_provider_sweep()) else 1

    if command in {"--inventory", "inventory", "--matrix", "matrix"}:
        _show_provider_configuration()
        return 0

    if command in {"--offline", "offline"}:
        validation_ok = asyncio.run(quick_offline_test())
        failover_ok = asyncio.run(failover_test())
        return 0 if validation_ok and failover_ok else 1

    if command in {"--pytest", "pytest"}:
        return 0 if full_test_suite() else 1

    if command in {"--provider", "provider"}:
        if len(sys.argv) < 3:
            print("Usage: fair_test_console.py --provider PROVIDER_ID")
            print("Known provider IDs:")
            for entry in LIVE_PROVIDERS.values():
                print(f"  {entry['provider_id']}")
            return 2
        provider_id = sys.argv[2].strip()
        try:
            _entry_for_provider(provider_id)
        except ValueError:
            print(f"Unknown provider: {provider_id}")
            return 2
        return 0 if asyncio.run(_run_selected_live_provider(provider_id)) else 1

    print(f"Unknown FAIR test mode: {sys.argv[1]}")
    print("Supported: --menu, --sweep, --inventory, --offline, --pytest, --provider ID")
    return 2


def main() -> int:
    command_status = _command_line_mode()
    if command_status is not None:
        return command_status

    while True:
        _header()
        choice = input("Choose 1-9: ").strip()
        try:
            if choice == "1":
                asyncio.run(quick_offline_test())
                _pause()
            elif choice == "2":
                asyncio.run(live_provider_test())
                _pause()
            elif choice == "3":
                asyncio.run(live_provider_sweep())
                _pause()
            elif choice == "4":
                asyncio.run(failover_test())
                _pause()
            elif choice == "5":
                asyncio.run(live_cross_check_test())
                _pause()
            elif choice == "6":
                full_test_suite()
                _pause()
            elif choice == "7":
                asyncio.run(show_live_providers())
                _pause()
            elif choice == "8":
                reset_confirmations()
                _pause()
            elif choice == "9":
                print("Exiting FAIR Test Console.")
                return 0
            else:
                print("Please choose a number from 1 to 9.")
        except KeyboardInterrupt:
            print("\nCancelled. Returning to menu.")


if __name__ == "__main__":
    raise SystemExit(main())
