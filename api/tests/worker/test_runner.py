"""The worker loop: waking, polling, concurrency, shutdown and liveness."""

import asyncio
import time

import pytest
import pytest_asyncio
from sqlalchemy.exc import OperationalError

from app.worker import __main__ as entrypoint
from app.worker import tile_photo
from app.worker.jobs import JobQueue
from app.worker.runner import Worker
from tests.worker.conftest import (
    add_photo,
    job_state,
    owner_in,
    photo_state,
)
from tests.worker.imaging import encode, gradient
from tests.worker.test_cleanup import HOUR, add_file, file_row

pytestmark = pytest.mark.asyncio


def jpeg() -> bytes:
    return encode(gradient(900, 700), "JPEG", quality=85)


async def until(condition, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return False


class Running:
    """A worker running as it does in the container, stopped cleanly afterwards."""

    def __init__(self, worker: Worker) -> None:
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(worker.run(self.stop))

    async def shutdown(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=15)


@pytest_asyncio.fixture
async def running(make_worker):
    started = []

    def start(**overrides) -> Running:
        started.append(Running(make_worker(**overrides)))
        return started[-1]

    yield start
    for item in started:
        if not item.task.done():
            await item.shutdown()


async def test_a_notification_wakes_an_idle_worker_long_before_the_next_poll(running, org, store):
    run = running(worker_poll_seconds=60)
    await asyncio.sleep(1.0)  # the listener has connected and the loops are waiting

    started = time.monotonic()
    photo = add_photo(org, store, jpeg())
    done = await until(lambda: photo_state(photo)[0] == "tiled", within=10)

    assert done
    assert time.monotonic() - started < 8  # the poll would have taken a minute
    await run.shutdown()


async def test_polling_finds_jobs_when_no_notification_arrives(running, org, store, monkeypatch):
    async def deaf(self, stop):
        await stop.wait()

    monkeypatch.setattr(Worker, "_listen", deaf)
    run = running(worker_poll_seconds=0.2)
    await asyncio.sleep(0.3)

    photo = add_photo(org, store, jpeg())

    assert await until(lambda: photo_state(photo)[0] == "tiled", within=10)
    await run.shutdown()


async def test_a_job_whose_retry_time_arrives_is_picked_up_by_polling(
    running, org, store, monkeypatch
):
    async def deaf(self, stop):
        await stop.wait()

    monkeypatch.setattr(Worker, "_listen", deaf)
    photo = add_photo(org, store, jpeg())
    with owner_in(org.id) as connection:
        connection.execute("UPDATE jobs SET run_after = now() + interval '1 second'")
    run = running(worker_poll_seconds=0.2)

    assert await until(lambda: photo_state(photo)[0] == "tiled", within=10)
    await run.shutdown()


async def test_concurrent_loops_each_photo_exactly_once(running, org, store):
    photos = [add_photo(org, store, jpeg()) for _ in range(6)]
    run = running(worker_concurrency=2)

    assert await until(lambda: all(photo_state(p)[0] == "tiled" for p in photos), within=30)

    await run.shutdown()
    assert [job_state(org.id, p.id)[:2] for p in photos] == [("succeeded", 1)] * 6


async def test_stopping_lets_the_job_in_hand_finish(make_worker, org, store, monkeypatch):
    photo = add_photo(org, store, jpeg())
    stop = asyncio.Event()
    real = tile_photo.TilePhoto.run

    async def stop_then_run(self, job):
        stop.set()  # told to stop while this job is in progress
        await real(self, job)

    monkeypatch.setattr(tile_photo.TilePhoto, "run", stop_then_run)

    await asyncio.wait_for(make_worker().run(stop), timeout=30)

    assert photo_state(photo)[0] == "tiled"
    assert job_state(org.id, photo.id)[:2] == ("succeeded", 1)


async def test_a_database_error_while_claiming_does_not_kill_the_loop(
    running, org, store, monkeypatch
):
    real = JobQueue.claim
    failures = []

    async def flaky(self):
        if len(failures) < 2:
            failures.append(1)
            raise OperationalError("SELECT 1", {}, Exception("connection lost"))
        return await real(self)

    monkeypatch.setattr(JobQueue, "claim", flaky)
    photo = add_photo(org, store, jpeg())
    run = running(worker_poll_seconds=0.2)

    assert await until(lambda: photo_state(photo)[0] == "tiled", within=15)
    assert len(failures) == 2
    await run.shutdown()


async def test_a_handler_that_crashes_leaves_the_job_for_a_retry(running, org, store, monkeypatch):
    async def boom(self, job):
        raise RuntimeError("bug in a handler")

    monkeypatch.setattr(tile_photo.TilePhoto, "run", boom)
    photo = add_photo(org, store, jpeg())
    run = running(worker_poll_seconds=0.2)

    # Queued again after one attempt: it was claimed, crashed, and was released with a backoff.
    assert await until(
        lambda: job_state(org.id, photo.id) == ("queued", 1, "unexpected"), within=10
    )
    await run.shutdown()


async def test_housekeeping_runs_on_its_interval(running, org, store):
    file_id = add_file(org, store, status="pending", age=2 * HOUR)
    run = running(cleanup_interval_seconds=1)

    assert await until(lambda: file_row(org, file_id) is None, within=15)
    await run.shutdown()


# Liveness --------------------------------------------------------------------------


async def test_the_worker_touches_its_heartbeat_file_and_the_check_reads_it(
    running, tmp_path, settings, monkeypatch
):
    beat = tmp_path / "alive"
    monkeypatch.setattr(
        entrypoint,
        "get_settings",
        lambda: settings.model_copy(update={"worker_heartbeat_file": beat}),
    )
    assert entrypoint.healthcheck() == 1  # no file yet: not alive

    run = running(worker_heartbeat_file=beat)

    assert await until(beat.exists, within=5)
    assert entrypoint.healthcheck() == 0
    await run.shutdown()


async def test_a_stale_heartbeat_is_unhealthy(tmp_path, settings, monkeypatch):
    beat = tmp_path / "alive"
    beat.touch()
    stale = time.time() - 120
    import os

    os.utime(beat, (stale, stale))
    monkeypatch.setattr(
        entrypoint,
        "get_settings",
        lambda: settings.model_copy(update={"worker_heartbeat_file": beat}),
    )

    assert entrypoint.healthcheck() == 1


async def test_a_job_that_runs_past_its_lock_is_given_up_on_and_retried(
    running, org, store, monkeypatch
):
    async def stuck(self, job):
        await asyncio.sleep(30)

    monkeypatch.setattr(tile_photo.TilePhoto, "run", stuck)
    photo = add_photo(org, store, jpeg())
    run = running(job_lock_seconds=1, worker_poll_seconds=0.2)

    assert await until(lambda: job_state(org.id, photo.id) == ("queued", 1, "timeout"), within=10)
    await run.shutdown()


async def test_housekeeping_does_not_run_on_every_enqueue(running, org, store, monkeypatch):
    from app.worker import runner
    from app.worker.cleanup import Cleanup

    runs = []
    real = Cleanup.run_once

    async def counted(self):
        runs.append(1)
        return await real(self)

    monkeypatch.setattr(Cleanup, "run_once", counted)
    monkeypatch.setattr(runner.random, "uniform", lambda a, b: 0.3)
    run = running(cleanup_interval_seconds=3600)
    assert await until(lambda: len(runs) == 1, within=5)

    photos = [add_photo(org, store, jpeg()) for _ in range(4)]  # four notifications
    assert await until(lambda: all(photo_state(p)[0] == "tiled" for p in photos), within=30)
    await asyncio.sleep(0.5)

    assert len(runs) == 1
    await run.shutdown()
