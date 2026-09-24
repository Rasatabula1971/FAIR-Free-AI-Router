import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "fair_test_console.py"


def _load_console():
    spec = importlib.util.spec_from_file_location("fair_test_console", CONSOLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_console_offline_validation_smoke():
    console = _load_console()
    assert await console.quick_offline_test()


@pytest.mark.asyncio
async def test_console_failover_smoke():
    console = _load_console()
    assert await console.failover_test()


def test_windows_launcher_exists():
    launcher = ROOT / "START_FAIR.bat"
    assert launcher.is_file()
    assert "fair_test_console.py" in launcher.read_text(encoding="utf-8")


def test_provider_configuration_status_does_not_expose_values(monkeypatch):
    console = _load_console()
    values = {"KILO_API_KEY": "super-secret"}
    monkeypatch.setattr(console, "_env_value", lambda name: values.get(name))

    configured, reason = console._provider_status(console.LIVE_PROVIDERS["3"])

    assert configured
    assert reason == "configured"
    assert "super-secret" not in reason


@pytest.mark.asyncio
async def test_selected_live_fair_isolates_requested_provider(monkeypatch):
    console = _load_console()
    monkeypatch.setattr(
        console,
        "_env_value",
        lambda name: "test-key" if name == "KILO_API_KEY" else None,
    )

    fair = console._selected_live_fair({"kilo_free"})
    try:
        assert [provider["provider_id"] for provider in fair.providers()] == ["kilo_free"]
    finally:
        await fair.close()


def test_reset_confirmations():
    console = _load_console()
    console._session_confirmed.update({"groq", "google_gemini_api"})

    console.reset_confirmations()

    assert console._session_confirmed == set()
