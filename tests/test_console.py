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
