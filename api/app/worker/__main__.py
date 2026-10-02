"""`python -m app.worker`: run the background worker until it is told to stop."""

import asyncio
import logging
import signal
import sys
import time

from app.config import get_settings
from app.db.engine import create_engine
from app.storage.s3 import ObjectStore
from app.worker.runner import Worker

HEALTHY_WITHIN_SECONDS = 30


async def run() -> None:
    settings = get_settings()
    # Workers and the cleanup each hold a connection for a moment; a few spare
    # cover the listener's neighbours without letting the pool grow unbounded.
    engine = create_engine(
        settings.worker_database_url(), pool_size=settings.worker_concurrency + 2, max_overflow=0
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        await Worker(settings, engine, ObjectStore(settings)).run(stop)
    finally:
        await engine.dispose()


def healthcheck() -> int:
    """Exit 0 if the worker touched its heartbeat file recently. Used by the container."""
    try:
        age = time.time() - get_settings().worker_heartbeat_file.stat().st_mtime
    except OSError:
        return 1
    return 0 if age < HEALTHY_WITHIN_SECONDS else 1


def main() -> None:
    if "--healthcheck" in sys.argv:
        raise SystemExit(healthcheck())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
