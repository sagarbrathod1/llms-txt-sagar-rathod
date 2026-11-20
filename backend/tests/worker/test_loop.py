"""The queue loop (app.worker.loop): claiming, stale recovery, scheduling, and its run modes."""

import asyncio

import pytest

from app import engine, queries
from app.engine import GenerationError
from app.worker import jobs, loop

# anyio's pytest plugin ships with FastAPI's dependencies and runs these async tests.
pytestmark = pytest.mark.anyio


async def wait_for_status(db, job_id, status: str, timeout: float = 5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while db.job(job_id)["status"] != status:
        assert asyncio.get_running_loop().time() < deadline, f"job never became {status}"
        await asyncio.sleep(0.02)


def make_stale(db, job_id, attempts: int) -> None:
    db.sql(
        "UPDATE jobs SET locked_at = now() - interval '1 hour', attempts = %s WHERE id = %s",
        (attempts, job_id),
    )


async def recover(pool) -> None:
    async with pool.connection() as conn:
        await loop.recover_stale_jobs(conn)


# --- claiming ---


async def test_claim_takes_user_jobs_first_then_the_oldest(db, claim):
    _, recheck = db.add_site("https://monitored.com", "recheck")  # queued first
    _, older = db.add_site("https://a.com", "generate")
    _, newer = db.add_site("https://b.com", "generate")

    # Someone is waiting on a user job; a recheck can wait.
    job = await claim()
    assert (job["id"], job["url"]) == (older, "https://a.com")
    row = db.job(older)
    assert (row["status"], row["attempts"]) == ("running", 1)
    assert row["started_at"] is not None and row["locked_at"] is not None

    assert [(await claim())["id"], (await claim())["id"]] == [newer, recheck]
    assert await claim() is None


async def test_concurrent_claims_get_different_jobs(db, pool):
    db.add_site("https://a.com", "generate")
    db.add_site("https://b.com", "generate")

    # Explicit transactions (the pool autocommits) that stay open, so the first claim still
    # holds its row lock during the second: SKIP LOCKED must move on to the other job rather
    # than wait or take the same one.
    async with pool.connection() as conn1, pool.connection() as conn2:
        async with conn1.transaction(), conn2.transaction():
            # If the claim ever waited on the lock, fail fast instead of hanging the test run.
            await conn2.execute("SET LOCAL lock_timeout = '2s'")
            job1 = await queries.claim_job(conn1)
            job2 = await queries.claim_job(conn2)
            async with pool.connection() as conn3, conn3.transaction():
                await conn3.execute("SET LOCAL lock_timeout = '2s'")
                job3 = await queries.claim_job(conn3)  # nothing left: None, without blocking
    assert job1["id"] != job2["id"]
    assert job3 is None


# --- stale-job recovery ---


async def test_stale_job_is_requeued_as_new_and_live_job_left_alone(db, pool, claim):
    _, stale = db.add_site("https://stale.com", "generate")
    _, live = db.add_site("https://live.com", "generate")
    await claim()
    await claim()  # locked_at = now(): its worker is alive
    make_stale(db, stale, attempts=1)
    db.sql("UPDATE jobs SET pages_discovered = 30, pages_crawled = 12 WHERE id = %s", (stale,))

    await recover(pool)

    job = db.job(stale)
    assert (job["status"], job["locked_at"], job["started_at"]) == ("queued", None, None)
    assert (job["pages_discovered"], job["pages_crawled"]) == (0, 0)
    assert job["attempts"] == 1  # a stale requeue does use up the attempt
    assert db.job(live)["status"] == "running"


async def test_stale_job_out_of_attempts_is_failed(db, pool, claim):
    site_id, job_id = db.add_site(job_kind="recheck")
    await claim()
    make_stale(db, job_id, attempts=loop.MAX_ATTEMPTS)

    await recover(pool)

    job = db.job(job_id)
    assert (job["status"], job["error"]) == ("failed", jobs.GENERIC_ERROR)
    site = db.site(site_id)
    assert site["consecutive_failures"] == 1
    assert site["next_check_at"] is not None  # rescheduled, not re-queued every tick


# --- scheduler ---


async def test_scheduler_queues_only_due_monitored_sites(db, pool):
    due, _ = db.add_site("https://due.com")
    not_due, _ = db.add_site("https://not-due.com")
    unmonitored, _ = db.add_site("https://unmonitored.com")
    never_checked, _ = db.add_site("https://never-checked.com")  # next_check_at NULL
    busy, _ = db.add_site("https://busy.com", "generate")
    for site_id in (due, unmonitored, busy):
        db.make_due(site_id)
    db.sql("UPDATE sites SET next_check_at = now() + interval '1 hour' WHERE id = %s", (not_due,))
    db.sql("UPDATE sites SET monitor_enabled = false WHERE id = %s", (unmonitored,))

    for _ in range(2):  # a second tick must not duplicate anything
        async with pool.connection() as conn:
            await loop.schedule_rechecks(conn)

    queued = db.sql("SELECT site_id, kind FROM jobs ORDER BY created_at")
    assert queued == [
        {"site_id": busy, "kind": "generate"},  # its active job blocked a recheck
        {"site_id": due, "kind": "recheck"},
    ]
    assert never_checked not in [job["site_id"] for job in queued]


# --- shutdown, and surviving bugs unattended ---


async def test_cancelled_tick_releases_job(db, pool, monkeypatch):
    started = asyncio.Event()

    async def slow(url, on_progress=None):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(engine, "generate_llms_txt", slow)
    _, job_id = db.add_site(job_kind="generate")
    task = asyncio.create_task(loop.tick(pool))
    await started.wait()

    task.cancel()  # the shutdown (SIGTERM, or the API stopping)
    with pytest.raises(asyncio.CancelledError):  # cancellation must not be swallowed
        await task
    # Handed straight back, so another worker can start it now, not after the stale timeout.
    job = db.job(job_id)
    assert (job["status"], job["attempts"], job["locked_at"]) == ("queued", 1, None)
    assert (job["started_at"], job["pages_discovered"]) == (None, 0)


async def test_stray_cancel_in_a_store_step_does_not_stop_the_loop(
    db, pool, monkeypatch, fake_engine
):
    """Regression: a CancelledError nobody requested, raised outside the engine call, used to
    end the loop task silently, while /health kept answering."""
    _, first = db.add_site("https://a.com", "generate")
    _, second = db.add_site("https://b.com", "generate")
    fake_engine()
    real_store_success = jobs.store_success
    calls = []

    async def store_success_once_cancelled(*args):
        calls.append(args)
        if len(calls) == 1:
            raise asyncio.CancelledError  # e.g. leaked by a library, with no shutdown going on
        return await real_store_success(*args)

    monkeypatch.setattr(jobs, "store_success", store_success_once_cancelled)
    monkeypatch.setattr(loop, "ERROR_RETRY_SECONDS", 0.01)
    task = asyncio.create_task(loop.run_embedded(pool, asyncio.Event()))
    try:
        await wait_for_status(db, second, "succeeded")
    finally:
        task.cancel()
        await asyncio.wait([task])
    assert db.job(first)["status"] == "running"  # left to stale recovery


# --- drain mode ---


async def test_drain_runs_the_backlog_stale_jobs_and_due_rechecks_then_returns(
    db, pool, claim, monkeypatch, make_result
):
    _, stale = db.add_site("https://stale.com", "generate")
    await claim()
    make_stale(db, stale, attempts=1)  # its worker died
    db.add_site("https://a.com", "generate")
    db.add_site("https://bad.com", "generate")
    due, _ = db.add_site("https://due.com")
    db.make_due(due)

    async def generate(url, on_progress=None):
        if url == "https://bad.com":
            raise GenerationError("Down.")
        return make_result(f"# {url}\n")

    monkeypatch.setattr(engine, "generate_llms_txt", generate)

    assert await loop.drain(pool) == {"succeeded": 3, "failed": 1}
    assert db.sql("SELECT id FROM jobs WHERE status IN ('queued', 'running')") == []
    assert db.job(stale)["status"] == "succeeded"
    assert db.versions(due) == ["# https://due.com\n"]  # the recheck ran


async def test_drain_time_limit_releases_running_job(db, pool, monkeypatch):
    async def slow(url, on_progress=None):
        await asyncio.sleep(60)

    monkeypatch.setattr(engine, "generate_llms_txt", slow)
    monkeypatch.setattr(loop, "DRAIN_TIME_LIMIT_SECONDS", 0.3)
    _, job_id = db.add_site(job_kind="generate")

    assert await loop.drain(pool) == {}  # returns instead of running on
    job = db.job(job_id)
    assert (job["status"], job["attempts"]) == ("queued", 1)  # left for the next run


def tick_failing(monkeypatch, failures: int | None):
    """Make loop.tick raise the first `failures` times (None: always)."""
    real_tick = loop.tick
    calls = []

    async def tick(pool):
        calls.append(pool)
        if failures is None or len(calls) <= failures:
            raise RuntimeError("database unreachable")
        return await real_tick(pool)

    monkeypatch.setattr(loop, "tick", tick)


@pytest.mark.parametrize(
    ("failures", "outcomes"),
    [
        (1, {"errored": 1, "succeeded": 1}),  # a blip: counted, and the drain carries on
        (None, {"errored": loop.MAX_CONSECUTIVE_DRAIN_ERRORS}),  # down: gives up, doesn't spin
    ],
)
async def test_drain_counts_tick_errors(db, pool, monkeypatch, fake_engine, failures, outcomes):
    db.add_site(job_kind="generate")
    fake_engine()
    tick_failing(monkeypatch, failures)
    assert await loop.drain(pool) == outcomes


async def test_drain_clears_client_keys_past_the_window(db, pool):
    site_id, _ = db.add_site()
    for age in ("61 minutes", "59 minutes"):
        db.sql(
            "INSERT INTO jobs (site_id, kind, status, client_key, created_at) "
            "VALUES (%s, 'generate', 'succeeded', '203.0.113.7', now() - %s::interval)",
            (site_id, age),
        )

    await loop.drain(pool)

    rows = db.sql("SELECT client_key FROM jobs ORDER BY created_at")
    # Older than the window: no longer counted, so forgotten. Newer: still counted.
    assert [row["client_key"] for row in rows] == [None, "203.0.113.7"]
