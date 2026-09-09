#!/usr/bin/env python3
"""Development entrypoint.

Production runs uvicorn or gunicorn directly:

    uvicorn celestra.main:app --host 0.0.0.0 --port 8000 --workers 1

Use a single worker: runs execute in-process and the SSE bus is per-process, so
a second worker would split a run's event stream. Scaling out means moving the
bus to Redis, which is a deliberate later step.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

# Make the app importable no matter where this script is invoked from, so
# `python /somewhere/else/run.py` behaves the same as running it in place.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from celestra.settings import ensure_dirs, get_settings  # noqa: E402


def _free_port(host: str, port: int) -> int:
    """Return the requested port, or the next free one.

    A stale server on the same port otherwise fails the bind with a traceback
    that reads nothing like "something else is already running".
    """
    for candidate in range(port, port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host if host != "0.0.0.0" else "", candidate))
                return candidate
            except OSError:
                continue
    return port


def main() -> None:
    settings = get_settings()
    ensure_dirs()
    port = _free_port(settings.host, settings.port)

    # 0.0.0.0 means "listen on every interface"; it is not an address a browser
    # can open. Print the one the user should actually click.
    banner = [
        "",
        f"  {settings.app_name} is running.",
        "",
        f"  Open:      http://localhost:{port}",
        f"  API docs:  http://localhost:{port}/docs",
        f"  Health:    http://localhost:{port}/healthz",
        "",
    ]
    creds = settings.credential_status()
    if not creds["llm"]:
        banner += [
            "  No LLM provider is configured, so synthesis runs in deterministic",
            "  mode. Set a provider in .env to enable model-written output.",
            "",
        ]
    if port != settings.port:
        banner.insert(2, f"  Port {settings.port} was busy, using {port} instead.")
    print("\n".join(banner), flush=True)

    uvicorn.run(
        "celestra.main:app",
        host=settings.host,
        port=port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
