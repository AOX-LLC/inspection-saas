"""The worker loop: claim a job, run its handler, record the outcome.

Workers wake when a job is enqueued (`LISTEN inspection_jobs`) and poll as a
fallback, so a lost notification or a job whose retry time has arrived is picked
up within one poll interval. After finishing a job a worker claims again at once.

A worker that dies mid-job simply stops. Its lock lapses and another claim takes
the job; handlers are written to be repeated.
"""

import asyncio
import contextlib
import logging
import os
import random
import secrets
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import psycopg
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.db.engine import create_session_factory
from app.storage.s3 import ObjectStore
from app.worker.cleanup import Cleanup
from app.worker.errors import JobError
from app.worker.jobs import ClaimedJob, JobQueue
from app.worker.tile_photo import TilePhoto

logger = logging.getLogger(__name__)

CHANNEL = "inspection_jobs"
HEARTBEAT_SECONDS = 10
RECONNECT_SECONDS = 2


class Handler(Protocol):
    kind: str

    async def run(self, job: ClaimedJob) -> None: ...

    async def on_failed(self, job: ClaimedJob, code: str) -> None: ...


class Worker:
    def __init__(self, settings: Settings, engine: AsyncEngine, store: ObjectStore) -> None:
        self._settings = settings
        self._engine = engine
        factory = create_session_factory(engine)
        tile = TilePhoto(factory, store, settings)
        self._handlers: Mapping[str, Handler] = {tile.kind: tile}
        self._cleanup = Cleanup(engine, factory, store, settings)
        # The random part makes a lock holder's name unguessable: complete and fail
        # act on a job only for the name that claimed it.
        self._name = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(6)}"
        self._wake = asyncio.Event()

    def _queue(self, index: int) -> JobQueue:
        return JobQueue(
            self._engine,
            worker_id=f"{self._name}-{index}",
            kinds=list(self._handlers),
            lock_seconds=self._settings.job_lock_seconds,
            backoff_seconds=self._settings.job_backoff_seconds,
        )

    # Running --------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Run until `stop` is set; jobs in progress are finished first."""
        loops = [
            asyncio.create_task(self._work(index, stop))
            for index in range(self._settings.worker_concurrency)
        ]
        background = [
            asyncio.create_task(self._listen(stop)),
            asyncio.create_task(self._maintain(stop)),
            asyncio.create_task(self._heartbeat(stop)),
        ]
        logger.info("worker %s started with %d loop(s)", self._name, len(loops))
        await asyncio.gather(*loops)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        logger.info("worker %s stopped", self._name)

    async def run_until_idle(self) -> int:
        """Process every job that is due, then return how many ran. For tests and scripts."""
        queue = self._queue(0)
        processed = 0
        while (job := await queue.claim()) is not None:
            await self._process(queue, job)
            processed += 1
        return processed

    async def _work(self, index: int, stop: asyncio.Event) -> None:
        queue = self._queue(index)
        while not stop.is_set():
            try:
                job = await queue.claim()
            except SQLAlchemyError:
                logger.exception("claim failed; will retry")
                job = None
            if job is None:
                await self._sleep(stop, self._settings.worker_poll_seconds)
                continue
            await self._process(queue, job)

    async def _process(self, queue: JobQueue, job: ClaimedJob) -> None:
        handler = self._handlers[job.kind]
        code, retryable = "", True
        try:
            # A hard stop at the lock's length. The thread doing a pathological
            # decode cannot be interrupted, but the loop gives up on it and the
            # job is released for another attempt rather than held forever.
            async with asyncio.timeout(self._settings.job_lock_seconds):
                await handler.run(job)
        except TimeoutError:
            code, retryable = "timeout", True
            logger.warning("job %s (%s) timed out", job.id, job.kind)
        except JobError as error:
            code, retryable = error.code, error.retryable
            logger.warning("job %s (%s) failed: %s", job.id, job.kind, code)
        except Exception:
            code = "unexpected"
            logger.exception("job %s (%s) raised", job.id, job.kind)
        if not code:
            await self._settle(queue.complete(job), job)
            logger.info("job %s (%s) done", job.id, job.kind)
            return
        status = await self._settle(queue.fail(job, code, retryable=retryable), job)
        if status == "failed":
            await handler.on_failed(job, code)

    async def _settle(self, outcome, job: ClaimedJob):
        """Awaits a queue call. If the database is down the lock lapses and the job is retried."""
        try:
            return await outcome
        except SQLAlchemyError:
            logger.exception("could not record the outcome of job %s", job.id)
            return None

    # Waking, housekeeping and liveness --------------------------------------------

    async def _sleep(self, stop: asyncio.Event, seconds: float) -> None:
        """Until a notification, a stop, or `seconds`, whichever is first. For the job loops."""
        waiting = {asyncio.ensure_future(self._wake.wait()), asyncio.ensure_future(stop.wait())}
        _, pending = await asyncio.wait(
            waiting, timeout=seconds, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        self._wake.clear()

    @staticmethod
    async def _sleep_until_stop(stop: asyncio.Event, seconds: float) -> None:
        """Until a stop or `seconds`. Housekeeping must not wake on every enqueue."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    async def _listen(self, stop: asyncio.Event) -> None:
        """Sets the wake event on every NOTIFY. Reconnects forever; polling covers any gap."""
        while not stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(
                    _conninfo(self._settings), autocommit=True
                ) as connection:
                    await connection.execute(f"LISTEN {CHANNEL}")
                    async for _ in connection.notifies():
                        self._wake.set()
            except (psycopg.Error, OSError):
                logger.warning("job listener lost its connection; polling until it returns")
            await self._sleep_until_stop(stop, RECONNECT_SECONDS)

    async def _maintain(self, stop: asyncio.Event) -> None:
        interval = self._settings.cleanup_interval_seconds
        # Workers started together should not all clean up together.
        await self._sleep_until_stop(stop, random.uniform(1, min(30, interval)))  # noqa: S311
        while not stop.is_set():
            try:
                await self._cleanup.run_once()
            except Exception:
                logger.exception("cleanup run failed")
            await self._sleep_until_stop(stop, interval)

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        """Touches a file the container's health check reads."""
        path: Path = self._settings.worker_heartbeat_file
        while not stop.is_set():
            with contextlib.suppress(OSError):
                await asyncio.to_thread(path.touch)
            await self._sleep_until_stop(stop, HEARTBEAT_SECONDS)


def _conninfo(settings: Settings) -> str:
    url = settings.worker_database_url()
    return psycopg.conninfo.make_conninfo(
        host=url.host, port=url.port, dbname=url.database, user=url.username, password=url.password
    )
