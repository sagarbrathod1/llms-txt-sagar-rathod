"""Every SQL statement the API and worker run, in one place.

Each function takes an open connection and runs one statement (or a tightly related pair).
Connections are in autocommit mode (see db.py), so a single statement commits on its own.
Wherever several statements must land together, they run inside `async with
conn.transaction()`: create_site_and_enqueue below, and the worker's multi-step writes.
Rows come back as dicts (dict_row, see db.py).

Lock ordering: the worker's final writes lock the job row, then the site row. Nothing else may
hold a site row lock while waiting on a job, or the two can deadlock. That's why the POST
/sites path below takes no lock on the site row.
"""

from typing import Literal
from uuid import UUID

from psycopg import AsyncConnection

# The columns JobResponse needs. Listed once so every job query returns the same shape.
JOB_COLUMNS = """
    id, site_id, kind, status, pages_discovered, pages_crawled, error, warnings,
    created_at, started_at, finished_at
"""


# --- API: submit -------------------------------------------------------------------------


# The per-client rate limit's window (api/limits.py). Also how long a job keeps its
# client_key: after this, a submission counts for nothing, so the address isn't kept.
SUBMISSION_WINDOW_SECONDS = 60 * 60


async def count_recent_submissions(conn: AsyncConnection, client_key: str) -> dict:
    """This client's submissions within the window, for POST /sites's rate limit.

    count: generate jobs this client created in the window (POST /sites sets client_key on
    every job it creates; scheduled rechecks have none). retry_after: seconds until the oldest
    of them leaves the window (NULL if there are none).
    """
    cur = await conn.execute(
        """
        SELECT count(*) AS count,
               extract(epoch FROM min(created_at) + make_interval(secs => %(window)s) - now())
                   AS retry_after
        FROM jobs
        WHERE client_key = %(key)s
          AND created_at > now() - make_interval(secs => %(window)s)
        """,
        {"key": client_key, "window": SUBMISSION_WINDOW_SECONDS},
    )
    return await cur.fetchone()


async def clear_old_client_keys(conn: AsyncConnection) -> int:
    """Null client_key on jobs older than the rate-limit window. Returns how many.

    Privacy: a client's address is needed only while it can still count against the limit,
    so it isn't kept with the job history after that. Cheap: the partial index on
    client_key IS NOT NULL holds only the last hour or two of submissions.
    """
    cur = await conn.execute(
        """
        UPDATE jobs SET client_key = NULL
        WHERE client_key IS NOT NULL
          AND created_at < now() - make_interval(secs => %s)
        """,
        (SUBMISSION_WINDOW_SECONDS,),
    )
    return cur.rowcount


async def get_or_create_site(conn: AsyncConnection, url: str, max_monitored_sites: int) -> UUID:
    """The site's id, inserting the site if it's new.

    A new site is monitored only while fewer than max_monitored_sites are; otherwise it's
    created unmonitored (its llms.txt still works, it just isn't re-checked). Concurrent
    inserts can overshoot the cap by a site or two, which is harmless.

    Deliberately not an upsert (ON CONFLICT DO UPDATE): that would lock an existing site's
    row, and the next statement (inserting the job) can wait on the worker, which locks the
    same site row when it finishes a job. Each would wait for the other: a deadlock.
    DO NOTHING takes no row lock; it also returns no row on conflict, hence the SELECT.
    Nothing else about an existing site changes: monitoring settings change only through PATCH.
    """
    cur = await conn.execute(
        """
        INSERT INTO sites (url, monitor_enabled)
        VALUES (%s, (SELECT count(*) FROM sites WHERE monitor_enabled) < %s)
        ON CONFLICT (url) DO NOTHING
        RETURNING id
        """,
        (url, max_monitored_sites),
    )
    row = await cur.fetchone()
    if row is None:
        cur = await conn.execute("SELECT id FROM sites WHERE url = %s", (url,))
        row = await cur.fetchone()
    return row["id"]


async def insert_job(
    conn: AsyncConnection, site_id: UUID, kind: str, client_key: str | None = None
) -> UUID | None:
    """Queue a job. Returns None if the site already has a queued or running job.

    No conflict target is named, so DO NOTHING applies to the partial unique index
    jobs_one_active_per_site_idx (one active job per site).
    """
    cur = await conn.execute(
        """
        INSERT INTO jobs (site_id, kind, client_key) VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (site_id, kind, client_key),
    )
    row = await cur.fetchone()
    return row["id"] if row else None


async def create_site_and_enqueue(
    conn: AsyncConnection, url: str, client_key: str, max_monitored_sites: int
) -> tuple[UUID, UUID | None]:
    """Everything POST /sites writes: the site (if new) and its generate job (or the active one).

    One transaction, so a new site and its first job appear together or not at all.
    Only queues work: monitoring settings change through PATCH alone, so "generate again"
    never silently re-enables monitoring the user turned off.
    The job records client_key, which is what the rate limit counts, so only jobs actually
    created use up a submission: returning the active job doesn't.
    """
    async with conn.transaction():
        site_id = await get_or_create_site(conn, url, max_monitored_sites)
        job_id = await enqueue_or_get_active_job(conn, site_id, client_key)
    return site_id, job_id


async def get_active_job_id(conn: AsyncConnection, site_id: UUID) -> UUID | None:
    cur = await conn.execute(
        "SELECT id FROM jobs WHERE site_id = %s AND status IN ('queued', 'running')",
        (site_id,),
    )
    row = await cur.fetchone()
    return row["id"] if row else None


async def enqueue_or_get_active_job(
    conn: AsyncConnection, site_id: UUID, client_key: str
) -> UUID | None:
    """The new generate job's id, or the site's already active job's id.

    INSERT ... ON CONFLICT DO NOTHING returns no row on conflict, so the active job is looked
    up in a second statement. It may finish in between (the lookup finds nothing), so the pair
    is tried twice. That makes a None result unlikely, not impossible; the caller handles it.
    """
    for _ in range(2):
        job_id = await insert_job(conn, site_id, "generate", client_key)
        if job_id is None:
            job_id = await get_active_job_id(conn, site_id)
        if job_id is not None:
            return job_id
    return None


# --- API: read and update ------------------------------------------------------------------


async def get_job(conn: AsyncConnection, job_id: UUID) -> dict | None:
    cur = await conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (job_id,))
    return await cur.fetchone()


async def get_latest_job(conn: AsyncConnection, site_id: UUID) -> dict | None:
    cur = await conn.execute(
        f"SELECT {JOB_COLUMNS} FROM jobs WHERE site_id = %s ORDER BY created_at DESC LIMIT 1",
        (site_id,),
    )
    return await cur.fetchone()


async def get_site(conn: AsyncConnection, site_id: UUID) -> dict | None:
    cur = await conn.execute(
        """
        SELECT id, url, monitor_enabled, check_interval_hours, last_checked_at, next_check_at,
               consecutive_failures, created_at
        FROM sites WHERE id = %s
        """,
        (site_id,),
    )
    return await cur.fetchone()


async def set_monitoring(
    conn: AsyncConnection, site_id: UUID, enabled: bool, max_monitored_sites: int
) -> Literal["updated", "not_found", "at_capacity"]:
    """Turn monitoring on or off.

    Turning it on is refused once max_monitored_sites are monitored, unless this site already
    is (so repeating the request is harmless). Turning it off always works.
    The failure count is reset too: otherwise re-enabling a site the worker switched off at
    the failure limit would switch it off again after its very next failure.
    """
    cur = await conn.execute(
        """
        UPDATE sites SET monitor_enabled = %(enabled)s, consecutive_failures = 0
        WHERE id = %(site_id)s
          AND (NOT %(enabled)s
               OR monitor_enabled
               OR (SELECT count(*) FROM sites WHERE monitor_enabled) < %(cap)s)
        """,
        {"enabled": enabled, "site_id": site_id, "cap": max_monitored_sites},
    )
    if cur.rowcount == 1:
        return "updated"
    # Nothing updated: either the site doesn't exist, or the cap refused it.
    return "at_capacity" if await get_site(conn, site_id) else "not_found"


async def get_latest_version(conn: AsyncConnection, site_id: UUID) -> dict | None:
    cur = await conn.execute(
        """
        SELECT id, site_id, created_at, content, content_sha256 FROM llms_versions
        WHERE site_id = %s ORDER BY created_at DESC LIMIT 1
        """,
        (site_id,),
    )
    return await cur.fetchone()


async def list_versions(conn: AsyncConnection, site_id: UUID) -> list[dict]:
    # Only ids and dates: the content of every version would make GET /sites/{id} heavy.
    cur = await conn.execute(
        "SELECT id, created_at FROM llms_versions WHERE site_id = %s ORDER BY created_at DESC",
        (site_id,),
    )
    return await cur.fetchall()


async def get_version(conn: AsyncConnection, site_id: UUID, version_id: UUID) -> dict | None:
    # Filtering on site_id too, so a version id is only reachable under its own site's URL.
    cur = await conn.execute(
        """
        SELECT id, site_id, created_at, content FROM llms_versions
        WHERE id = %s AND site_id = %s
        """,
        (version_id, site_id),
    )
    return await cur.fetchone()


# --- Worker: queue maintenance ----------------------------------------------------------------


async def requeue_stale_jobs(
    conn: AsyncConnection, stale_seconds: int, max_attempts: int
) -> list[UUID]:
    """Put running jobs whose heartbeat stopped (worker died) back in the queue."""
    # Cleared so the API shows a plain queued job, not a half-started one. The crawl starts
    # over, so its progress does too.
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'queued', locked_at = NULL, started_at = NULL,
            pages_discovered = 0, pages_crawled = 0
        WHERE status = 'running' AND locked_at < now() - make_interval(secs => %s)
          AND attempts < %s
        RETURNING id
        """,
        (stale_seconds, max_attempts),
    )
    return [row["id"] for row in await cur.fetchall()]


async def fail_stale_jobs(
    conn: AsyncConnection, stale_seconds: int, max_attempts: int, error: str
) -> list[dict]:
    """Give up on stale jobs that already used all their attempts. Returns their id, site_id."""
    cur = await conn.execute(
        """
        UPDATE jobs SET status = 'failed', error = %s, finished_at = now(), locked_at = NULL
        WHERE status = 'running' AND locked_at < now() - make_interval(secs => %s)
          AND attempts >= %s
        RETURNING id, site_id
        """,
        (error, stale_seconds, max_attempts),
    )
    return await cur.fetchall()


async def enqueue_due_rechecks(conn: AsyncConnection) -> int:
    """Queue a recheck for every monitored site whose next check is due. Returns how many.

    A due site stays due until its job finishes, so this runs into the same site every tick
    while its job is queued or running. ON CONFLICT DO NOTHING (the one-active-job index)
    skips those, and also sites with a user-submitted job in progress.

    Known race, accepted: when two loops run at once (e.g. the embedded loop and the drain),
    one can queue a second recheck for a site whose job the other has just finished, because
    its snapshot still shows the site as due. The cost is one redundant crawl. Locking the site
    row first would invert the lock order (see the module docstring) and risk deadlocks.
    """
    cur = await conn.execute(
        """
        INSERT INTO jobs (site_id, kind)
        SELECT id, 'recheck' FROM sites WHERE monitor_enabled AND next_check_at <= now()
        ON CONFLICT DO NOTHING
        """
    )
    return cur.rowcount


async def claim_job(conn: AsyncConnection) -> dict | None:
    """Take the next queued job and mark it running. None if the queue is empty.

    User jobs (generate) go before scheduled rechecks, oldest first within each: a person is
    waiting on a generate job, while a recheck running a few minutes late costs nothing.
    Rechecks can't starve: each client's submissions are capped (see api/limits.py).

    FOR UPDATE SKIP LOCKED: a row another worker is claiming right now is skipped rather than
    waited for, so concurrent workers never take the same job or block each other.
    The caller must commit straight away, so the running status is visible and the lock freed.
    """
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'running', started_at = now(), locked_at = now(), attempts = attempts + 1
        FROM sites
        WHERE jobs.id = (
            SELECT id FROM jobs WHERE status = 'queued'
            ORDER BY (kind = 'recheck'), created_at LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
          AND sites.id = jobs.site_id
        RETURNING jobs.id, jobs.site_id, jobs.kind, jobs.attempts, sites.url
        """
    )
    return await cur.fetchone()


# --- Worker: while a job runs -----------------------------------------------------------------
#
# Every write about a running job is guarded by `status = 'running' AND attempts = <claimed>`.
# If the job was declared stale and requeued (and maybe claimed again, which bumps attempts),
# the guard matches nothing, so this worker can't overwrite or finish someone else's run.


async def heartbeat(
    conn: AsyncConnection, job_id: UUID, attempts: int, pages_discovered: int, pages_crawled: int
) -> None:
    """Refresh locked_at (proof of life) and flush the latest progress counters."""
    await conn.execute(
        """
        UPDATE jobs SET locked_at = now(), pages_discovered = %s, pages_crawled = %s
        WHERE id = %s AND status = 'running' AND attempts = %s
        """,
        (pages_discovered, pages_crawled, job_id, attempts),
    )


async def mark_succeeded(
    conn: AsyncConnection,
    job_id: UUID,
    attempts: int,
    pages_discovered: int,
    pages_crawled: int,
    warnings: list[str],
) -> bool:
    """Returns False if the guard matched nothing (the job was requeued meanwhile)."""
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'succeeded', finished_at = now(), locked_at = NULL, warnings = %s,
            pages_discovered = %s, pages_crawled = %s
        WHERE id = %s AND status = 'running' AND attempts = %s
        """,
        (warnings, pages_discovered, pages_crawled, job_id, attempts),
    )
    return cur.rowcount == 1


async def mark_failed(
    conn: AsyncConnection,
    job_id: UUID,
    attempts: int,
    pages_discovered: int,
    pages_crawled: int,
    error: str,
) -> bool:
    """Returns False if the guard matched nothing (the job was requeued meanwhile)."""
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'failed', finished_at = now(), locked_at = NULL, error = %s,
            pages_discovered = %s, pages_crawled = %s
        WHERE id = %s AND status = 'running' AND attempts = %s
        """,
        (error, pages_discovered, pages_crawled, job_id, attempts),
    )
    return cur.rowcount == 1


async def release_job(conn: AsyncConnection, job_id: UUID, attempts: int) -> bool:
    """Hand a running job back to the queue because this worker is shutting down.

    The attempt still counts: the next claim bumps attempts again, so no late write of ours
    can match the new run's guard. Returns False if the guard matched nothing.
    """
    cur = await conn.execute(
        """
        UPDATE jobs
        SET status = 'queued', locked_at = NULL, started_at = NULL,
            pages_discovered = 0, pages_crawled = 0
        WHERE id = %s AND status = 'running' AND attempts = %s
        """,
        (job_id, attempts),
    )
    return cur.rowcount == 1


async def insert_version(
    conn: AsyncConnection, site_id: UUID, job_id: UUID, content: str, content_sha256: str
) -> None:
    await conn.execute(
        """
        INSERT INTO llms_versions (site_id, job_id, content, content_sha256)
        VALUES (%s, %s, %s, %s)
        """,
        (site_id, job_id, content, content_sha256),
    )


# --- Worker: site schedule after a job finishes ----------------------------------------------
#
# Both outcomes push next_check_at forward. On failure that matters most: otherwise a failing
# site would still be due, and the scheduler would re-crawl it on every tick.


async def record_site_success(conn: AsyncConnection, site_id: UUID) -> None:
    await conn.execute(
        """
        UPDATE sites
        SET consecutive_failures = 0, last_checked_at = now(),
            next_check_at = now() + make_interval(hours => check_interval_hours)
        WHERE id = %s
        """,
        (site_id,),
    )


async def record_site_failure(conn: AsyncConnection, site_id: UUID, max_failures: int) -> None:
    """Count the failure, and turn monitoring off once max_failures is reached in a row."""
    # Expressions on the right of SET see the row's old values, hence "+ 1" in both places.
    await conn.execute(
        """
        UPDATE sites
        SET consecutive_failures = consecutive_failures + 1,
            monitor_enabled = monitor_enabled AND consecutive_failures + 1 < %s,
            last_checked_at = now(),
            next_check_at = now() + make_interval(hours => check_interval_hours)
        WHERE id = %s
        """,
        (max_failures, site_id),
    )
