"""Running one claimed job: the engine call, its heartbeat, and recording the outcome."""

import asyncio
import hashlib
import logging
from typing import Literal

from psycopg_pool import AsyncConnectionPool

from app import engine, queries
from app.engine import GenerationError, GenerationResult, Progress

log = logging.getLogger(__name__)

# What happened to a job this worker ran. "discarded": the job was requeued while it ran
# (declared stale), so the attempts guard rejected our result and nothing was written.
JobOutcome = Literal["succeeded", "failed", "discarded"]

# How often a running job's locked_at (and progress counters) are refreshed.
HEARTBEAT_SECONDS = 5

# Upper bound on one engine run, so a pathological site can't occupy the worker forever.
# Generous next to the engine's own page cap and per-request timeouts.
JOB_TIMEOUT_SECONDS = 10 * 60

# Failed jobs in a row before monitoring is switched off for a site. Without auth anyone can
# enrol any site, so a dead or blocking site must not be crawled forever. With daily checks,
# 5 tolerates a few days of outage.
MAX_CONSECUTIVE_FAILURES = 5

# On a recheck, a new version is skipped if more than this share of the pages the engine
# tried (GenerationResult.pages_attempted) failed: errors, refusals (401/403) or pages cut off
# by the engine's time limit. An outage, a block or a truncated run would otherwise look like
# pages vanishing from the site.
MAX_FAILED_PAGE_RATIO = 0.10

# Shown for any exception that isn't a GenerationError: those are bugs, and their text can
# contain internals (hostnames, file paths), so the detail goes to the log only.
GENERIC_ERROR = "Something went wrong while generating the llms.txt. Please try again later."


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def versioning_decision(
    kind: str, result: GenerationResult, content_sha256: str, latest_sha256: str | None
) -> tuple[bool, str | None]:
    """Whether to store the result as a new version, and a warning if versioning was skipped.

    Unchanged content is silently not stored: no change is the normal outcome of a recheck.
    The failed-pages check compares pages_failed with pages_attempted, every page the engine
    requested (not just those that made it into the file, which is only a selection).
    It applies only to rechecks of a site that already has a version:
    a user who asked for a generation gets its result, and with no earlier version there is
    no "change" to misreport.
    """
    if content_sha256 == latest_sha256:
        return False, None
    if (
        kind == "recheck"
        and latest_sha256 is not None
        and result.pages_failed > MAX_FAILED_PAGE_RATIO * result.pages_attempted
    ):
        return False, (
            f"{result.pages_failed} of {result.pages_attempted} pages couldn't be fetched, so "
            "this check didn't save a new version, in case the site was only partly reachable."
        )
    return True, None


async def store_success(
    pool: AsyncConnectionPool, job: dict, result: GenerationResult, progress: Progress
) -> bool:
    """Record a successful run. All writes in one transaction, so they land together or not.

    Returns False if the job was requeued meanwhile and the result was discarded.
    """
    content_sha256 = sha256(result.llms_txt)
    async with pool.connection() as conn, conn.transaction():
        latest = await queries.get_latest_version(conn, job["site_id"])
        store, warning = versioning_decision(
            job["kind"], result, content_sha256, latest["content_sha256"] if latest else None
        )
        warnings = result.warnings + ([warning] if warning else [])
        if not await queries.mark_succeeded(
            conn,
            job["id"],
            job["attempts"],
            progress.pages_discovered,
            progress.pages_crawled,
            warnings,
        ):
            log.warning("Job %s was requeued while running; discarding its result", job["id"])
            return False  # nothing was written, so the transaction commits as a no-op
        if store:
            await queries.insert_version(
                conn, job["site_id"], job["id"], result.llms_txt, content_sha256
            )
        await queries.record_site_success(conn, job["site_id"])
    return True


async def store_failure(
    pool: AsyncConnectionPool, job: dict, progress: Progress, error: str
) -> bool:
    """Record a failed run. Returns False if the job was requeued meanwhile (discarded)."""
    async with pool.connection() as conn, conn.transaction():
        if not await queries.mark_failed(
            conn,
            job["id"],
            job["attempts"],
            progress.pages_discovered,
            progress.pages_crawled,
            error,
        ):
            log.warning("Job %s was requeued while running; discarding its failure", job["id"])
            return False
        await queries.record_site_failure(conn, job["site_id"], MAX_CONSECUTIVE_FAILURES)
    return True


async def run_job(pool: AsyncConnectionPool, job: dict) -> JobOutcome:
    """Run the engine for a claimed job and record the outcome.

    If this task is cancelled (shutdown), CancelledError propagates and nothing is recorded;
    the caller (loop.tick) hands the job back to the queue.
    """
    log.info(
        "Running %s job %s for %s (attempt %d)", job["kind"], job["id"], job["url"], job["attempts"]
    )
    latest_progress = Progress(pages_discovered=0, pages_crawled=0)

    def on_progress(progress: Progress) -> None:
        # Sync and instant: only remember the latest counts. The heartbeat writes them.
        nonlocal latest_progress
        latest_progress = progress

    async def heartbeat() -> None:
        # On a timer, not on progress: a slow phase (say, a huge sitemap) reports no progress
        # for a while, but the worker is alive and must not look stale.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with pool.connection() as conn:
                    await queries.heartbeat(
                        conn,
                        job["id"],
                        job["attempts"],
                        latest_progress.pages_discovered,
                        latest_progress.pages_crawled,
                    )
            except Exception:
                # A database blip must not kill the heartbeat for the rest of the job.
                log.exception("Heartbeat failed for job %s", job["id"])

    heartbeat_task = asyncio.create_task(heartbeat())
    error = None
    try:
        async with asyncio.timeout(JOB_TIMEOUT_SECONDS) as limit:
            result = await engine.generate_llms_txt(job["url"], on_progress)
    except GenerationError as e:
        # The engine's own message is written to be shown to users.
        error = str(e)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise  # we are being cancelled: shutdown (see loop.tick)
        # Nobody cancelled us, so the engine leaked a CancelledError of its own: a bug. Fail
        # the job now with the generic message, like any other bug, rather than leaving it
        # running for stale recovery to retry.
        log.exception("Job %s: engine raised CancelledError without a shutdown", job["id"])
        error = GENERIC_ERROR
    except Exception:
        if limit.expired():
            # Our own time limit. Checked this way, not with `except TimeoutError`, because a
            # TimeoutError raised inside the engine would be a bug, not this limit.
            error = f"The site took longer than {JOB_TIMEOUT_SECONDS // 60} minutes to crawl."
        else:
            # A bug: full traceback in the log, a generic message for the user.
            log.exception("Job %s crashed", job["id"])
            error = GENERIC_ERROR
    finally:
        # Stop the heartbeat before writing the outcome, so it can't write afterwards.
        # asyncio.wait (not `await heartbeat_task`) waits without raising the heartbeat's own
        # CancelledError, yet still raises if *this* task is cancelled meanwhile (a shutdown),
        # so that is never swallowed.
        heartbeat_task.cancel()
        await asyncio.wait([heartbeat_task])

    if error is None:
        try:
            if not await store_success(pool, job, result, latest_progress):
                return "discarded"
            log.info("Job %s succeeded", job["id"])
            return "succeeded"
        except Exception:
            # E.g. output Postgres rejects (a NUL byte in the text). Left running, the job
            # would be re-crawled by stale recovery up to its attempt limit, failing each time.
            log.exception("Storing the result of job %s failed", job["id"])
            error = GENERIC_ERROR
    if not await store_failure(pool, job, latest_progress, error):
        return "discarded"
    log.info("Job %s failed: %s", job["id"], error)
    return "failed"
