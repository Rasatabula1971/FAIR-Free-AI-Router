import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest
import uvicorn
from conftest import provider

from apps.api.main import create_app
from fair.providers.mock import MockAdapter


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_javascript_client_against_real_http_api(make_router):
    router = make_router([(provider(), MockAdapter("a", "4"))], cache_enabled=True)
    app = create_app(router, {"alice": "node-test-key"}, "node-admin-key")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none", lifespan="on"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [shutil.which("node"), "--test"],
                cwd=Path(__file__).resolve().parents[1] / "sdk" / "javascript",
                env=os.environ | {"FAIR_SDK_TEST_URL": f"http://127.0.0.1:{port}"},
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()
