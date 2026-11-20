-- Initial schema: sites, the jobs queue, and llms.txt versions.
--
-- IDs are random UUIDs because they appear in public URLs (/sites/{id}/llms.txt): they don't
-- reveal how many sites exist and can't be walked one by one. gen_random_uuid() is built into
-- Postgres 13+, so no extension is needed.
--
-- Enumerations (kind, status) are text + CHECK rather than Postgres ENUM types: same safety,
-- and adding a value later is a one-line constraint change instead of ALTER TYPE.

-- One row per website, keyed by its normalized origin. Holds the monitoring schedule.
CREATE TABLE sites (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    url                  text NOT NULL UNIQUE,  -- normalized origin, e.g. https://example.com
    monitor_enabled      boolean NOT NULL DEFAULT true,
    check_interval_hours integer NOT NULL DEFAULT 24 CHECK (check_interval_hours > 0),
    next_check_at        timestamptz,           -- NULL until the site's first job finishes
    last_checked_at      timestamptz,
    -- Failed jobs in a row; reset on success. After a fixed limit the worker turns monitoring
    -- off: without auth anyone can enrol any site, so a dead or blocking site must not be
    -- crawled forever.
    consecutive_failures integer NOT NULL DEFAULT 0,
    created_at           timestamptz NOT NULL DEFAULT now()
);

-- The work queue. Workers claim rows with SELECT ... FOR UPDATE SKIP LOCKED.
CREATE TABLE jobs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id          uuid NOT NULL REFERENCES sites (id) ON DELETE CASCADE,
    kind             text NOT NULL CHECK (kind IN ('generate', 'recheck')),
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    pages_discovered integer NOT NULL DEFAULT 0,
    pages_crawled    integer NOT NULL DEFAULT 0,
    error            text,
    -- Non-fatal engine warnings (e.g. "robots.txt disallows /docs"), shown to the user.
    warnings         text[] NOT NULL DEFAULT '{}',
    attempts         integer NOT NULL DEFAULT 0,
    -- Who submitted a generate job (POST /sites): the client's IPv4 address, or its IPv6 /64
    -- network. Used only to rate-limit submissions; NULL for scheduled rechecks, and cleared
    -- once older than the rate-limit window (1 hour) by the hourly drain.
    client_key       text,
    -- Set when a worker claims the job and refreshed by the worker's heartbeat timer. A running
    -- job whose locked_at is old belongs to a worker that died, so it can be requeued.
    locked_at        timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz
);

-- Queue claim: "oldest queued user job, else oldest queued recheck". The key matches the
-- claim's ORDER BY ((kind = 'recheck'), created_at) exactly, so LIMIT 1 reads one index entry.
-- Partial, so it only holds the few queued rows, not the whole job history.
CREATE INDEX jobs_queued_idx ON jobs ((kind = 'recheck'), created_at) WHERE status = 'queued';

-- Stale-job recovery: "running jobs whose locked_at is too old".
CREATE INDEX jobs_running_locked_at_idx ON jobs (locked_at) WHERE status = 'running';

-- At most one active job per site, so two clicks (or a click racing the scheduler) can't
-- crawl the same site twice. POST /sites and the scheduler insert with
-- ON CONFLICT DO NOTHING RETURNING id, which returns no row on conflict, so the caller then
-- SELECTs the active job in a second statement. That job may finish between the two
-- statements (the SELECT finds nothing), so the caller retries the pair once. This is not
-- race-free: one retry makes a failure unlikely, not impossible. The index also serves
-- "find this site's active job".
CREATE UNIQUE INDEX jobs_one_active_per_site_idx ON jobs (site_id)
    WHERE status IN ('queued', 'running');

-- Rate limiting POST /sites ("this client's submissions in the last hour") and the hourly
-- cleanup that nulls client_key once it is older than that window.
-- Partial on client_key IS NOT NULL, so it only ever holds the last hour or two of
-- submissions (rows leave it when their key is nulled): both queries stay cheap however long
-- the history gets.
CREATE INDEX jobs_client_key_created_idx ON jobs (created_at, client_key)
    WHERE client_key IS NOT NULL;

-- "A site's latest job", whatever its status: the UI shows it after a reload.
CREATE INDEX jobs_site_created_idx ON jobs (site_id, created_at DESC);

-- Generated llms.txt files. A row is inserted only when the content differs from the site's
-- latest version, so this table is the site's change history.
CREATE TABLE llms_versions (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id        uuid NOT NULL REFERENCES sites (id) ON DELETE CASCADE,
    -- UNIQUE: a job produces at most one version, so a retried "store result" step can't
    -- insert a duplicate.
    job_id         uuid NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE CASCADE,
    content        text NOT NULL,
    -- Compared with the latest version's hash to decide whether anything changed. Deliberately
    -- not unique per site: if a site changes and then changes back (A -> B -> A), that's two
    -- real changes and both belong in the history.
    content_sha256 text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- "Latest version of a site" and "versions of a site, newest first".
CREATE INDEX llms_versions_site_created_idx ON llms_versions (site_id, created_at DESC);

-- Scheduler: "monitored sites whose next check is due". Partial, so unmonitored sites
-- aren't in it.
CREATE INDEX sites_due_idx ON sites (next_check_at) WHERE monitor_enabled;
