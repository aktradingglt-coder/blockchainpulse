"""``python -m rd_db.healthcheck <service>`` — exit 0 if a heartbeat for ``service`` from this host is fresh.

Used as the container healthcheck for the worker and scheduler.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from datetime import timedelta

from rd_core.redaction import redact_text
from rd_core.settings import get_settings
from rd_db.engine import make_engine
from rd_db.heartbeat import is_fresh_for_host

MAX_AGE = timedelta(seconds=45)


async def check(service: str, host: str) -> bool:
    engine = make_engine(get_settings(), pool_size=1)
    try:
        return await is_fresh_for_host(engine, service, host, MAX_AGE)
    finally:
        await engine.dispose()


def main() -> None:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: python -m rd_db.healthcheck <service>\n")
        sys.exit(2)
    try:
        healthy = asyncio.run(check(sys.argv[1], socket.gethostname()))
    except Exception as exc:  # noqa: BLE001 - a healthcheck reports, it never dumps a traceback into health logs
        sys.stdout.write(f"unhealthy: {type(exc).__name__}: {redact_text(str(exc))}\n")
        sys.exit(1)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
