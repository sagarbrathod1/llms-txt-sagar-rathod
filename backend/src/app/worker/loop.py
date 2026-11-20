"""The queue loop: recover stale jobs, schedule due rechecks, claim a job and run it.

tick() is one round. run_forever(), run_embedded() and drain() repeat it in the ways the
worker's run modes need (see app.worker.main). Several loops may run at once, in any mix of
modes: claiming with FOR UPDATE SKIP LOCKED means two loops never take the same job. (Their
schedulers can race in one known, accepted way; see queries.enqueue_due_rechecks.)
"""

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Literal

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app import queries
from app.worker.jobs import GENERIC_ERROR, MAX_CONSECUTIVE_FAILURES, JobOutcome, run_job

log = logging.getLogger(__name__)

# A tick that raised instead of finishing (logged; see safe_tick).
TickResult = JobOutcome | Literal["errored"] | None

# Dedicated mode: how long to wait before polling again when the queue is empty. Each idle
# poll is one pool checkout running three cheap indexed queries; a shorter wait makes a new
# job start sooner. A user waits at most this long for their job to start, small next to a
# ~1 minute crawl. A polling loop never lets Neon autosuspend, which is fine for an always-on
# deployment and is why the $0 one uses the embedded mode (no polling) and drains instead.
IDLE_POLL_SECONDS = 3

# Embedded mode: the loop is woken by POST /sites, so this fallback tick only catches what
# the wake-up can't see: work left after a failed tick, or rechecks falling due while the
# machine happens to be up (the hourly drain covers those too). Longer than Neon's 5-minute
# suspend delay, so a machine kept up only by read traffic still lets the database sleep.
EMBEDDED_FALLBACK_SECONDS = 10 * 60

# After a tick that errored, wait this long and tick again, rather than waiting for new work:
# the queue may still hold jobs that nothing will announce again (e.g. the rest of a batch
# after one job hit a bug).
ERROR_RETRY_SECONDS = 5

# A running job whose locked_at is older than this belongs to a dead worker. Many missed
# heartbeats, so a briefly slow database or a busy event loop doesn't trigger a false alarm.
STALE_AFTER_SECONDS = 120

# Total claims per job (the first run plus retries) before a stale job is given up on. Small,
# because a job that keeps killing its worker (e.g. running out of memory) would keep doing so.
MAX_ATTEMPTS = 3

# Drain mode: errors in a row before giving up, e.g. the database is unreachable. A failed
# job is an outcome, not an error; errors are ticks that raised. Stopping early beats
# hammering a dead database until the time limit; the next hourly run tries again.
MAX_CONSECUTIVE_DRAIN_ERRORS = 3

# Upper bound on one drain run. Drains are scheduled hourly, so 50 minutes keeps a backlog
# from making one run overlap the next indefinitely, with margin for shutdown. Several
# 10-minute jobs still fit, and whatever is left waits for the next run.
DRAIN_TIME_LIMIT_SECONDS = 50 * 60


async def recover_stale_jobs(conn: AsyncConnection) -> None:
    async with conn.transaction():
        requeued = await queries.requeue_stale_jobs(conn, STALE_AFTER_SECONDS, MAX_ATTEMPTS)
        failed = await queries.fail_stale_jobs(
            conn, STALE_AFTER_SECONDS, MAX_ATTEMPTS, GENERIC_ERROR
        )
        # A job given up on counts as a failed check, like any other failure, so the site's
        # schedule advances instead of the scheduler requeueing it on the next tick.
        for job in failed:
            await queries.record_site_failure(conn, job["site_id"], MAX_CONSECUTIVE_FAILURES)
    for job_id in requeued:
        log.warning("Requeued stale job %s", job_id)
    for job in failed:
        log.warning("Gave up on stale job %s after %d attempts", job["id"], MAX_ATTEMPTS)


async def schedule_rechecks(conn: AsyncConnection) -> None:
    count = await queries.enqueue_due_rechecks(conn)
    if count:
        log.info("Queued %d recheck(s)", count)


async def claim_job(conn: AsyncConnection) -> dict | None:
    # One statement on an autocommit connection: committed before the (long) crawl starts,
    # so the claim is visible and the row unlocked straight away.
    return await queries.claim_job(conn)


async def release_job(pool: AsyncConnectionPool, job: dict) -> None:
    """On shutdown, put the current job back in the queue (see tick)."""
    try:
        async with pool.connection() as conn:
            released = await queries.release_job(conn, job["id"], job["attempts"])
        if released:
            log.info("Released job %s back to the queue", job["id"])
        else:
            log.info("Job %s is no longer ours (finished or requeued); left alone", job["id"])
    except Exception:
        # Must not raise: an exception here would replace the CancelledError and the loop
        # would carry on instead of shutting down. Stale recovery will requeue the job.
        log.exception("Couldn't release job %s", job["id"])


async def tick(pool: AsyncConnectionPool) -> JobOutcome | None:
    """One round of the loop. Returns the outcome of the job it ran, or None if none was queued.

    Cancelling the task running this is the shutdown path for every mode: SIGTERM for the
    dedicated and drain processes, the API's shutdown for the embedded loop, the time limit
    for a drain. The current job is abandoned rather than finished, because a crawl can
    outlast a host's shutdown grace period, and handed straight back to the queue.
    """
    # One pool checkout for all three steps: when idle, this is the whole tick, every few
    # seconds. Each step commits on its own.
    async with pool.connection() as conn:
        await recover_stale_jobs(conn)
        await schedule_rechecks(conn)
        # A shutdown during the claim's COMMIT can leave a claimed job that tick never learns
        # about, so it isn't released below. Stale recovery requeues it after its timeout.
        job = await claim_job(conn)
    if job is None:
        return None
    try:
        return await run_job(pool, job)
    except asyncio.CancelledError:
        # Shutting down mid-job: hand the job straight back so another worker can start it
        # now, rather than after stale recovery's timeout. Only for a real cancellation of
        # this task; a stray CancelledError is a bug (see safe_tick). Guarded, so a job that
        # already finished is left alone. Not shielded: only a second cancel could interrupt
        # it, and then stale recovery still covers the job.
        if asyncio.current_task().cancelling():
            await release_job(pool, job)
        raise


async def safe_tick(pool: AsyncConnectionPool) -> TickResult:
    """tick(), but an error is logged and reported as "errored" instead of raised.

    Only a real cancellation of this task (a shutdown) still propagates. A CancelledError
    nobody requested (cancelling() == 0) is a bug that leaked from somewhere, and is treated
    like any other error: re-raising it would silently end the loop while /health still
    reports OK. Its job, if any, is left running and stale recovery requeues it.
    """
    try:
        return await tick(pool)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        log.exception("Worker tick ended with a CancelledError nobody requested")
        return "errored"
    except Exception:
        # E.g. the database is briefly unreachable.
        log.exception("Worker tick failed")
        return "errored"


async def run_ticks(pool: AsyncConnectionPool, wait_for_work: Callable[[], Awaitable]) -> None:
    """Tick back-to-back while jobs run; once idle, wait_for_work(); after an error, retry soon."""
    while True:
        result = await safe_tick(pool)
        if result == "errored":
            await asyncio.sleep(ERROR_RETRY_SECONDS)
        elif result is None:
            await wait_for_work()


async def run_forever(pool: AsyncConnectionPool) -> None:
    """Dedicated mode: poll for work every few seconds until cancelled."""
    log.info("Worker loop started (dedicated)")
    await run_ticks(pool, lambda: asyncio.sleep(IDLE_POLL_SECONDS))


async def run_embedded(pool: AsyncConnectionPool, work_queued: asyncio.Event) -> None:
    """Embedded mode: tick at startup, then when `work_queued` is set (by POST /sites), or
    after EMBEDDED_FALLBACK_SECONDS. Runs until cancelled (the API's shutdown).

    Needs no supervisor: safe_tick turns every error into "errored", so the loop itself
    can't die while the API keeps answering.
    """

    async def wait_for_work() -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(work_queued.wait(), EMBEDDED_FALLBACK_SECONDS)
        # Cleared before the next tick: anything queued from here on sets it again, so no
        # wake-up is lost. (POST sets it only after its job is committed, so the tick that
        # follows can claim that job.)
        work_queued.clear()

    log.info("Worker loop started (embedded)")
    await run_ticks(pool, wait_for_work)


async def drain(pool: AsyncConnectionPool) -> Counter[str]:
    """Run ticks until no job is queued, then return how they ended (drain mode).

    Each tick also recovers stale jobs and queues due rechecks, so a drain started on a
    schedule does monitoring as well as any backlog. First, it clears client keys that are
    past the rate-limit window (privacy). A tick that errors is counted and the
    drain carries on, unless MAX_CONSECUTIVE_DRAIN_ERRORS happen in a row. The caller exits
    non-zero if any tick errored, so failures show up in the host's logs.
    """
    outcomes: Counter[str] = Counter()
    errors_in_a_row = 0
    try:
        # Privacy housekeeping, here because the drain is the one thing that runs every hour
        # whatever the traffic. A failure is counted like a tick error, and the drain goes on.
        try:
            async with pool.connection() as conn:
                cleared = await queries.clear_old_client_keys(conn)
            if cleared:
                log.info("Cleared the client key of %d old job(s)", cleared)
        except Exception:
            log.exception("Clearing old client keys failed")
            outcomes["errored"] += 1
        async with asyncio.timeout(DRAIN_TIME_LIMIT_SECONDS) as limit:
            while (result := await safe_tick(pool)) is not None:
                outcomes[result] += 1
                errors_in_a_row = errors_in_a_row + 1 if result == "errored" else 0
                if errors_in_a_row >= MAX_CONSECUTIVE_DRAIN_ERRORS:
                    log.error("Drain giving up after %d errors in a row", errors_in_a_row)
                    break
    except TimeoutError:
        if not limit.expired():
            raise  # not our limit: a bug, so let it surface
        # The running job (if any) was released by tick's cancellation path.
        log.warning(
            "Drain stopped at its %d-minute limit; queued jobs wait for the next run",
            DRAIN_TIME_LIMIT_SECONDS // 60,
        )
    finally:
        # In finally, so even a drain cut short by a shutdown leaves its summary in the log.
        log.info(
            "Drain finished: %d succeeded, %d failed, %d discarded, %d errored",
            outcomes["succeeded"],
            outcomes["failed"],
            outcomes["discarded"],
            outcomes["errored"],
        )
    return outcomes
