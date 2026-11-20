"""Running one claimed job (app.worker.jobs): the outcome, the heartbeat, and the guards."""

import asyncio

import pytest

from app import engine, queries
from app.engine import GenerationError, Progress
from app.worker import jobs, loop

# anyio's pytest plugin ships with FastAPI's dependencies and runs these async tests.
pytestmark = pytest.mark.anyio


async def queue_and_run(pool, db, claim, site_id=None, kind: str = "recheck"):
    """Queue a job (on a new site, or the given one), claim it and run it to the end."""
    if site_id is None:
        site_id, job_id = db.add_site(job_kind=kind)
    else:
        job_id = db.add_job(site_id, kind)
    await jobs.run_job(pool, await claim())
    return site_id, job_id


# --- the versioning rule (pure); the stored outcome is in test_workflows.py ---


@pytest.mark.parametrize(
    ("kind", "content", "latest", "pages", "failed", "attempted", "store"),
    [
        ("recheck", "# Same\n", "# Same\n", 10, 0, None, False),  # unchanged: silently skipped
        ("recheck", "# New\n", "# Old\n", 9, 1, None, True),  # exactly 10% failed: stored
        ("recheck", "# New\n", "# Old\n", 8, 2, None, False),  # 20% failed: skipped, warned
        ("recheck", "# New\n", None, 8, 2, None, True),  # no earlier version to misreport
        ("generate", "# New\n", "# Old\n", 8, 2, None, True),  # the user asked: always stored
        # The share is of pages attempted (40), not the few the file lists (5): 3 failures are
        # 7.5%, not 3 of 8. Regression: it was once computed against the listed pages.
        ("recheck", "# New\n", "# Old\n", 5, 3, 40, True),
    ],
)
def test_versioning_decision(make_result, kind, content, latest, pages, failed, attempted, store):
    new = make_result(content, pages=pages, failed=failed, attempted=attempted)
    latest_sha = jobs.sha256(latest) if latest else None
    stored, warning = jobs.versioning_decision(kind, new, jobs.sha256(content), latest_sha)
    assert stored is store
    # Only a skip for failed pages warns; an unchanged file is the normal outcome.
    assert (warning is not None) == (not store and content != latest)


# --- outcomes and the site's schedule ---


async def test_failures_are_counted_and_a_success_resets_them(pool, db, claim, fake_engine):
    fake_engine(error=GenerationError("Down."))
    site_id, _ = await queue_and_run(pool, db, claim)
    site = db.site(site_id)
    assert site["consecutive_failures"] == 1
    assert site["monitor_enabled"] is True
    assert site["next_check_at"] is not None  # rescheduled, not retried on every tick

    fake_engine()
    await queue_and_run(pool, db, claim, site_id)
    site = db.site(site_id)
    assert site["consecutive_failures"] == 0
    hours = (site["next_check_at"] - site["last_checked_at"]).total_seconds() / 3600
    assert hours == site["check_interval_hours"]


async def test_monitoring_turns_off_at_failure_limit(pool, db, claim, fake_engine):
    site_id, _ = db.add_site()
    db.sql(
        "UPDATE sites SET consecutive_failures = %s WHERE id = %s",
        (jobs.MAX_CONSECUTIVE_FAILURES - 1, site_id),
    )
    fake_engine(error=GenerationError("Down."))
    await queue_and_run(pool, db, claim, site_id)

    site = db.site(site_id)
    assert site["consecutive_failures"] == jobs.MAX_CONSECUTIVE_FAILURES
    assert site["monitor_enabled"] is False


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (GenerationError("The site didn't respond."), "The site didn't respond."),  # for users
        (RuntimeError("secret internal detail"), jobs.GENERIC_ERROR),  # a bug: details to the log
        # Raised inside the engine, so a bug too, not the job's own time limit.
        (TimeoutError("socket read"), jobs.GENERIC_ERROR),
    ],
)
async def test_failed_job_records_a_safe_message_and_its_progress(
    pool, db, claim, monkeypatch, error, message
):
    async def generate(url, on_progress=None):
        on_progress(Progress(pages_discovered=9, pages_crawled=4))
        raise error

    monkeypatch.setattr(engine, "generate_llms_txt", generate)
    _, job_id = await queue_and_run(pool, db, claim)

    job = db.job(job_id)
    assert (job["status"], job["error"]) == ("failed", message)
    assert (job["pages_discovered"], job["pages_crawled"]) == (9, 4)


async def test_engine_timeout_fails_job(pool, db, claim, monkeypatch):
    async def slow(url, on_progress=None):
        await asyncio.sleep(5)

    monkeypatch.setattr(jobs, "JOB_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(engine, "generate_llms_txt", slow)
    _, job_id = await queue_and_run(pool, db, claim)

    job = db.job(job_id)
    assert job["status"] == "failed"
    assert "took longer" in job["error"]


async def test_error_while_storing_result_fails_job(pool, db, claim, fake_engine):
    """Regression: an exception inside store_success left the job running, so stale recovery
    re-crawled it up to its attempt limit, failing the same way each time."""
    # Postgres text can't hold a NUL byte, so the version insert raises inside store_success.
    fake_engine("# Bad \x00 byte\n")
    site_id, job_id = await queue_and_run(pool, db, claim)

    job = db.job(job_id)
    assert (job["status"], job["error"]) == ("failed", jobs.GENERIC_ERROR)
    assert db.versions(site_id) == []


# --- the heartbeat and the attempts guard ---


async def test_heartbeat_writes_progress_and_refreshes_lock(
    pool, db, claim, monkeypatch, make_result
):
    seen = {}

    async def generate(url, on_progress=None):
        on_progress(Progress(pages_discovered=7, pages_crawled=3))
        before = db.job(job_id)["locked_at"]
        await asyncio.sleep(0.2)  # several heartbeats
        seen.update(db.job(job_id), locked_at_before=before)
        return make_result()

    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(engine, "generate_llms_txt", generate)
    _, job_id = db.add_site(job_kind="generate")
    await jobs.run_job(pool, await claim())

    assert (seen["pages_discovered"], seen["pages_crawled"]) == (7, 3)
    assert seen["locked_at"] > seen["locked_at_before"]


@pytest.mark.parametrize("fails", [False, True])
async def test_requeued_job_is_not_finished_twice(pool, db, claim, monkeypatch, make_result, fails):
    """Regression: a job declared stale and requeued while its first run was still going was
    finished by both runs. Every write is now guarded by the attempt number of the claim."""
    site_id, job_id = db.add_site(job_kind="recheck")
    job = await claim()
    # Meanwhile the job was declared stale, requeued and claimed by another worker.
    db.sql("UPDATE jobs SET attempts = attempts + 1 WHERE id = %s", (job_id,))
    locked_at = db.job(job_id)["locked_at"]

    async def generate(url, on_progress=None):
        on_progress(Progress(pages_discovered=50, pages_crawled=50))
        await asyncio.sleep(0.1)  # the old run's heartbeats try to write meanwhile
        if fails:
            raise GenerationError("Down.")
        return make_result("# Late\n")

    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(engine, "generate_llms_txt", generate)
    assert await jobs.run_job(pool, job) == "discarded"

    # Nothing written: the job, its lock and progress, the site and its versions are left to
    # the newer claim.
    row = db.job(job_id)
    assert (row["status"], row["locked_at"], row["pages_discovered"]) == ("running", locked_at, 0)
    site = db.site(site_id)
    assert (site["last_checked_at"], site["consecutive_failures"]) == (None, 0)
    assert db.versions(site_id) == []


# --- cancellation: a real shutdown versus a leaked CancelledError ---


async def test_engine_leaking_cancelled_error_is_a_bug_not_a_shutdown(db, pool, fake_engine):
    """Regression (the poison job): nobody cancels the worker here, so this is an engine bug.
    Treated as a shutdown, the job was handed back to the queue and the next worker hit the
    same bug. It must fail at once with the generic message."""
    _, job_id = db.add_site(job_kind="generate")
    fake_engine(error=asyncio.CancelledError())

    try:
        outcome = await loop.tick(pool)
    except asyncio.CancelledError:
        # Caught explicitly: a CancelledError escaping a test isn't reported as a failure.
        pytest.fail("a CancelledError nobody asked for was treated as a shutdown")
    assert outcome == "failed"
    job = db.job(job_id)
    assert (job["status"], job["error"], job["attempts"]) == ("failed", jobs.GENERIC_ERROR, 1)


async def test_shutdown_while_the_heartbeat_stops_is_not_swallowed(
    pool, db, monkeypatch, make_result
):
    """Regression: the wait for the heartbeat to stop swallowed every CancelledError, including
    a SIGTERM arriving during that wait, and then went on to store the result."""
    engine_returned = asyncio.Event()

    async def heartbeat_slow_to_stop(conn, job_id, *args):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)  # e.g. a slow network write finishing up
            raise

    async def generate(url, on_progress=None):
        await asyncio.sleep(0.1)  # let the heartbeat get into its slow call
        engine_returned.set()
        return make_result()

    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(queries, "heartbeat", heartbeat_slow_to_stop)
    monkeypatch.setattr(engine, "generate_llms_txt", generate)
    _, job_id = db.add_site(job_kind="generate")
    task = asyncio.create_task(loop.tick(pool))
    await engine_returned.wait()
    await asyncio.sleep(0.05)  # run_job is now waiting for the heartbeat to stop

    task.cancel()  # the SIGTERM
    with pytest.raises(asyncio.CancelledError):
        await task
    # The result wasn't stored: the job was handed back to the queue instead.
    assert db.job(job_id)["status"] == "queued"


# --- lock ordering ---


async def test_post_and_worker_final_write_do_not_deadlock(pool, db, claim):
    """Regression: POST /sites used to lock the site row (upsert) and then wait on the job,
    while the worker held the job and then needed the site row."""
    site_id, job_id = db.add_site(job_kind="generate")
    job = await claim()

    async def post_sites() -> object:
        async with pool.connection() as conn:
            _, new_job = await queries.create_site_and_enqueue(
                conn, "https://example.com", "1.2.3.4", max_monitored_sites=100
            )
            return new_job

    # The worker's final write, in store_success's order.
    async with pool.connection() as conn, conn.transaction():
        assert await queries.mark_succeeded(conn, job_id, job["attempts"], 0, 0, [])
        post = asyncio.create_task(post_sites())
        await asyncio.sleep(0.3)  # POST is now waiting on this job's unique-index entry
        # Fail fast instead of hanging if this ever waits on POST again.
        await conn.execute("SET LOCAL lock_timeout = '3s'")
        await queries.record_site_success(conn, site_id)

    new_job_id = await asyncio.wait_for(post, timeout=5)
    assert new_job_id != job_id  # POST saw the old job finish and queued a new one
