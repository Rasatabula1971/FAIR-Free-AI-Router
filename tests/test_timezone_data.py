"""FAIR must import on hosts that ship no system IANA time-zone database."""

import os
import subprocess
import sys
import tempfile


def test_import_does_not_depend_on_system_timezone_data():
    # An empty TZPATH stands in for Windows: only the `tzdata` package can
    # supply America/Los_Angeles, which FAIR constructs at import time.
    with tempfile.TemporaryDirectory() as empty:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from fair import FAIR; from zoneinfo import ZoneInfo; ZoneInfo('America/Los_Angeles')",
            ],
            env={**os.environ, "PYTHONTZPATH": empty},
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.returncode == 0, result.stderr
