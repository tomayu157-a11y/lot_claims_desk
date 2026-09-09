#!/usr/bin/env python3
"""Development entrypoint.

Production runs uvicorn or gunicorn directly:

    uvicorn celestra.main:app --host 0.0.0.0 --port 8000 --workers 1

Use a single worker: runs execute in-process and the SSE bus is per-process, so
a second worker would split a run's event stream. Scaling out means moving the
bus to Redis, which is a deliberate later step.
"""
from __future__ import annotations

import argparse
import socket
import subprocess
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


def _revision() -> str:
    """Short git revision, so a stale checkout is visible at a glance."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        rev = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return f"{rev}{'+local changes' if dirty else ''}" if rev else "unknown"
    except Exception:
        return "unknown"


def self_check() -> int:
    """Verify the app can actually serve its pages, and say what is wrong if not.

    Catches the two failures that look identical from a browser: a stale
    checkout whose routes raise, and a working server reached at the wrong
    address.
    """
    import asyncio
    import logging

    # The check's own output is the point; library chatter buries it.
    logging.disable(logging.INFO)

    problems: list[str] = []
    print(f"\n  Celestra self-check  (revision {_revision()})\n")

    for name, path in (("templates", ROOT / "celestra" / "templates"),
                       ("stylesheet", ROOT / "celestra" / "static" / "css" / "app.css"),
                       ("javascript", ROOT / "celestra" / "static" / "js" / "app.js")):
        ok = path.exists()
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}: {path}")
        if not ok:
            problems.append(f"{name} missing at {path}")

    try:
        import httpx

        from celestra.main import app

        async def probe() -> list[tuple[str, int]]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://check") as c:
                out = []
                for path in ("/", "/projects", "/projects/new", "/settings", "/healthz"):
                    out.append((path, (await c.get(path)).status_code))
                return out

        for path, status in asyncio.run(probe()):
            ok = status == 200
            print(f"  {'ok  ' if ok else 'FAIL'}  GET {path} -> {status}")
            if not ok:
                problems.append(f"GET {path} returned {status}")
    except Exception as exc:
        print(f"  FAIL  app failed to load: {type(exc).__name__}: {exc}")
        problems.append(f"app import/render failed: {exc}")

    print()
    if problems:
        print("  Not healthy:")
        for p in problems:
            print(f"    - {p}")
        print("\n  Most likely a stale checkout. Run:")
        print("    git pull && pip install -r requirements.txt\n")
        return 1
    print("  All checks passed. Start the server with:  python run.py\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Celestra server.")
    parser.add_argument("--check", action="store_true",
                        help="verify the app can serve its pages, then exit")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(self_check())

    settings = get_settings()
    ensure_dirs()
    port = _free_port(settings.host, settings.port)

    # 0.0.0.0 means "listen on every interface"; it is not an address a browser
    # can open. Print the one the user should actually click.
    banner = [
        "",
        f"  {settings.app_name} is running.  (revision {_revision()})",
        "",
        "  Open the first URL in a browser. Do not use 0.0.0.0 - that is the",
        "  bind address, not a reachable one.",
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
