#!/usr/bin/env python3
"""Development entrypoint. Production runs uvicorn or gunicorn directly:

    uvicorn celestra.main:app --host 0.0.0.0 --port 8000 --workers 1

Use a single worker: runs are executed in-process and the SSE bus is
per-process, so multiple workers would split a run's event stream. Scaling out
means moving the bus to Redis, which is a deliberate later step.
"""
from __future__ import annotations

import uvicorn

from celestra.settings import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "celestra.main:app",
        host=s.host,
        port=s.port,
        reload=s.debug,
        log_level="debug" if s.debug else "info",
    )


if __name__ == "__main__":
    main()
