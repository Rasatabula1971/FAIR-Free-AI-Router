"""Run the local FAIR HTTP service."""

import os


def _port() -> int:
    raw = os.environ.get("FAIR_SERVICE_PORT", "8000")
    try:
        value = int(raw)
    except ValueError as error:
        raise SystemExit("FAIR_SERVICE_PORT must be an integer") from error
    if not 1 <= value <= 65535:
        raise SystemExit("FAIR_SERVICE_PORT must be between 1 and 65535")
    return value


def main():
    try:
        import uvicorn
    except ImportError as error:
        raise SystemExit('Install the service extra: pip install -e ".[service]"') from error

    uvicorn.run(
        "fair.service.app:create_app_from_env",
        factory=True,
        host=os.environ.get("FAIR_SERVICE_HOST", "127.0.0.1"),
        port=_port(),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
