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

REQUIRED = [
    # import name, pip name
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("jinja2", "jinja2"),
    ("httpx", "httpx"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("yaml", "pyyaml"),
    ("multipart", "python-multipart"),
    ("selectolax", "selectolax"),
]


def missing_dependencies() -> list[tuple[str, str]]:
    import importlib.util

    return [
        (mod, pkg) for mod, pkg in REQUIRED
        if importlib.util.find_spec(mod) is None
    ]


def _dependency_help(missing: list[tuple[str, str]]) -> str:
    names = ", ".join(pkg for _, pkg in missing)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    lines = [
        "",
        f"  Missing {len(missing)} dependency/dependencies: {names}",
        "",
        "  Install them with:",
        "",
        "    pip install -r requirements.txt",
        "",
    ]
    if not in_venv:
        lines += [
            "  You are not in a virtual environment. On most systems the command",
            "  above needs one:",
            "",
            "    python -m venv .venv",
            "    source .venv/bin/activate        # Windows: .venv\\Scripts\\activate",
            "    pip install -r requirements.txt",
            "",
        ]
    return "\n".join(lines)


def _port_taken_by(host: str, port: int) -> str | None:
    """Identify what is already on the port, so a stale server is obvious.

    Returns a short description, or None when the port is free. A previous
    Celestra left running is the single most confusing failure mode: the new
    process moves to another port while the browser keeps hitting the old one,
    which may be serving an older, broken build.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host if host != "0.0.0.0" else "", port))
            return None
        except OSError:
            pass
    try:
        import httpx

        resp = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
        if resp.status_code == 200 and "connectors" in resp.text:
            return "another Celestra server"
        return f"an HTTP server (returned {resp.status_code})"
    except Exception:
        return "something else"


def _free_port(host: str, port: int) -> int:
    """The requested port, or the next free one."""
    for candidate in range(port, port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host if host != "0.0.0.0" else "", candidate))
                return candidate
            except OSError:
                continue
    return port


def setup() -> int:
    """Prepare a local checkout: dependencies, .env, data directories."""
    print(f"\n  Celestra setup  (revision {_revision()})\n")

    missing = missing_dependencies()
    if missing:
        print("  FAIL  dependencies")
        print(_dependency_help(missing))
        return 1
    print(f"  ok    dependencies ({len(REQUIRED)} packages)")

    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        print(f"  ok    .env already exists ({env})")
    elif example.exists():
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  ok    created {env} from .env.example")
        print("        Every value in it is optional. Fill in an LLM provider to")
        print("        enable model-written synthesis.")
    else:
        print("  warn  no .env.example to copy; the app runs on defaults")

    from celestra.settings import ensure_dirs as _ensure

    _ensure()
    print("  ok    data directories")
    print("\n  Next:\n    python run.py --demo     # seed a run so the UI has content")
    print("    python run.py            # start the server\n")
    return 0


def demo() -> int:
    """Seed a completed run offline so every screen has content."""
    from celestra.settings import ensure_dirs as _ensure

    _ensure()
    print("\n  Seeding an offline demo run. No network, no credentials.\n")
    from celestra.demo import seed_sync

    try:
        run = seed_sync()
    except Exception as exc:
        print(f"  FAIL  {type(exc).__name__}: {exc}\n")
        return 1

    from celestra.store import store

    questions = store.get_questions(run.id)
    answered = sum(1 for q in questions if q.status.value == "sufficient")
    print(f"  Run {run.reference} — {run.status.value}")
    print(f"    {answered}/{len(questions)} questions answered")
    print(f"    {len(store.get_evidence(run.id))} evidence items")
    print(f"    {len(store.get_insights(run.id))} findings")
    print(f"    {len(store.get_contradictions(run.id))} source conflicts")
    print("\n  Start the server and open it:\n")
    print("    python run.py")
    from celestra.settings import get_settings as _gs

    print(f"    http://localhost:{_gs().port}/runs/{run.id}/overview\n")
    return 0


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
    from celestra.settings import ensure_dirs, get_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Run the Celestra server.")
    parser.add_argument("--setup", action="store_true",
                        help="install check, create .env, prepare data directories")
    parser.add_argument("--check", action="store_true",
                        help="verify the app can serve its pages, then exit")
    parser.add_argument("--demo", action="store_true",
                        help="seed a completed run offline, then exit")
    args = parser.parse_args()

    missing = missing_dependencies()
    if missing and not args.setup:
        print(_dependency_help(missing))
        raise SystemExit(1)

    if args.setup:
        raise SystemExit(setup())
    if args.check:
        raise SystemExit(self_check())
    if args.demo:
        raise SystemExit(demo())

    settings = get_settings()
    ensure_dirs()
    occupant = _port_taken_by(settings.host, settings.port)
    port = _free_port(settings.host, settings.port)

    banner: list[str] = ["", f"  {settings.app_name} is running.  (revision {_revision()})", ""]

    if occupant:
        stale = occupant == "another Celestra server"
        banner += [
            "  " + "!" * 68,
            f"  PORT {settings.port} IS ALREADY IN USE by {occupant}.",
            f"  This server is on {port} instead.",
            "",
        ]
        if stale:
            banner += [
                f"  http://localhost:{settings.port} is the OLD server, which may be",
                "  running older code. Either use the address below, or stop the old",
                "  one first:",
                "",
                f"    macOS/Linux:  lsof -ti:{settings.port} | xargs kill",
                f"    Windows:      netstat -ano | findstr :{settings.port}"
                "   then  taskkill /PID <pid> /F",
            ]
        else:
            banner += [f"  Use the address below, or free port {settings.port}."]
        banner += ["  " + "!" * 68, ""]

    banner += [
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

    print("\n".join(banner), flush=True)

    import uvicorn

    uvicorn.run(
        "celestra.main:app",
        host=settings.host,
        port=port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
