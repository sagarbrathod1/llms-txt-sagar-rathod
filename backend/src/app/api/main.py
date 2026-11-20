"""HTTP API. Handlers stay thin: they validate input, call app.queries, and shape responses.

Run locally with: uv run uvicorn app.api.main:app --reload
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from psycopg_pool import AsyncConnectionPool

from app import queries
from app.api import limits
from app.api.schemas import (
    CreateSiteRequest,
    CreateSiteResponse,
    HealthResponse,
    JobResponse,
    SiteResponse,
    UpdateSiteRequest,
    VersionResponse,
    VersionSummary,
)
from app.config import configure_logging, settings
from app.db import create_pool
from app.engine.urls import normalize_origin
from app.worker.loop import run_embedded

log = logging.getLogger(__name__)

# How long shutdown waits for the embedded worker to hand its job back. Under the host's kill
# timeout (about 10 s on Fly), which also has to cover uvicorn finishing in-flight requests.
# If it runs out, the job is left to stale recovery.
EMBEDDED_SHUTDOWN_SECONDS = 5


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Set by POST /sites when it queues a job, to wake this process's embedded worker.
    app.state.work_queued = asyncio.Event()
    # One pool per process, opened at startup and closed at shutdown.
    async with create_pool() as pool:
        app.state.pool = pool
        # Embedded worker mode (app.worker.main lists the modes): the job loop runs in this
        # process, on this pool, for as long as the API is up. The tradeoff: Fly stops the
        # machine when no HTTP traffic arrives, and the loop stops with it. During a crawl
        # the web app polls the job every 1.5 s, which keeps the machine up; if the user
        # leaves mid-crawl the machine may stop, and the job is picked up again when it next
        # wakes (or by the hourly drain).
        # Assumes one API process on one machine. More would still be correct (SKIP LOCKED),
        # but the wake-up event is per process: a job queued on one machine wakes only that
        # machine's loop, and the others see it at their fallback tick.
        worker = (
            asyncio.create_task(run_embedded(pool, app.state.work_queued))
            if settings.embedded_worker
            else None
        )
        try:
            yield
        finally:
            if worker is not None:
                # The loop's shutdown path, as SIGTERM is for a dedicated worker: the
                # cancelled loop hands its current job back to the queue (loop.tick). Wait for
                # that before the pool closes, but not past the host's kill timeout.
                # asyncio.wait doesn't re-raise the loop's own CancelledError.
                worker.cancel()
                done, _ = await asyncio.wait([worker], timeout=EMBEDDED_SHUTDOWN_SECONDS)
                if not done:
                    log.warning("Embedded worker didn't stop in time; stale recovery will")


app = FastAPI(title="llms.txt generator", lifespan=lifespan)

# The browser calls this API directly from the web app's origin (no proxy), so it needs CORS.
# Only the configured origins; no credentials, because there are no cookies or auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type"],
)


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} not found")


@app.get("/health")
async def health() -> HealthResponse:
    # Deliberately doesn't touch the database. The host calls this every few seconds while
    # the machine runs; a query here would keep the database awake (and billed) even when
    # nobody uses the app. "The process answers HTTP" is what the host needs to know.
    return HealthResponse(status="ok")


# 202 Accepted: the request only queues work; the client polls GET /jobs/{job_id}.
@app.post("/sites", status_code=202)
async def create_site(body: CreateSiteRequest, request: Request) -> CreateSiteResponse:
    try:
        url = normalize_origin(body.url)
    except ValueError as e:
        # normalize_origin's messages are written for users, so pass them through.
        raise HTTPException(status_code=422, detail=str(e)) from None

    key = limits.client_key(limits.client_ip(request))
    async with get_pool(request).connection() as conn:
        # After validation, so a typo doesn't use up a submission. Only jobs actually created
        # count: returning a site's active job records no new submission.
        limits.check_submission_rate(await queries.count_recent_submissions(conn, key))
        site_id, job_id = await queries.create_site_and_enqueue(
            conn, url, key, limits.MAX_MONITORED_SITES
        )
    if job_id is None:
        # Two quick job turnovers lost the race twice; asking the client to retry is honest.
        raise HTTPException(status_code=503, detail="The site is busy, please try again.")
    # Wake the embedded worker, if this process runs one, so the job starts at once. Set after
    # the job is committed, so the worker's claim can see it. Also set when an existing active
    # job was returned: that costs one extra, cheap tick.
    request.app.state.work_queued.set()
    return CreateSiteResponse(site_id=site_id, job_id=job_id)


@app.get("/jobs/{job_id}")
async def get_job(job_id: UUID, request: Request) -> JobResponse:
    async with get_pool(request).connection() as conn:
        job = await queries.get_job(conn, job_id)
    if job is None:
        raise not_found("Job")
    return JobResponse(**job)


async def load_site(request: Request, site_id: UUID) -> SiteResponse:
    """The full site view, shared by GET and PATCH so both return the same shape."""
    async with get_pool(request).connection() as conn:
        site = await queries.get_site(conn, site_id)
        if site is None:
            raise not_found("Site")
        latest_job = await queries.get_latest_job(conn, site_id)
        latest_version = await queries.get_latest_version(conn, site_id)
        versions = await queries.list_versions(conn, site_id)
    return SiteResponse(
        **site,
        latest_job=JobResponse(**latest_job) if latest_job else None,
        latest_version=VersionResponse(**latest_version) if latest_version else None,
        versions=[VersionSummary(**v) for v in versions],
    )


@app.get("/sites/{site_id}")
async def get_site(site_id: UUID, request: Request) -> SiteResponse:
    return await load_site(request, site_id)


@app.patch("/sites/{site_id}")
async def update_site(site_id: UUID, body: UpdateSiteRequest, request: Request) -> SiteResponse:
    async with get_pool(request).connection() as conn:
        result = await queries.set_monitoring(
            conn, site_id, body.monitor_enabled, limits.MAX_MONITORED_SITES
        )
    if result == "not_found":
        raise not_found("Site")
    if result == "at_capacity":
        # 409: the request is valid, but conflicts with the current state (the cap is full).
        raise HTTPException(status_code=409, detail=limits.monitoring_at_capacity())
    return await load_site(request, site_id)


@app.get("/sites/{site_id}/versions/{version_id}")
async def get_version(site_id: UUID, version_id: UUID, request: Request) -> VersionResponse:
    async with get_pool(request).connection() as conn:
        version = await queries.get_version(conn, site_id, version_id)
    if version is None:
        raise not_found("Version")
    return VersionResponse(**version)


# The stable, shareable URL: always serves the site's latest version as plain text.
@app.get("/sites/{site_id}/llms.txt", response_class=PlainTextResponse)
async def get_latest_llms_txt(site_id: UUID, request: Request) -> str:
    async with get_pool(request).connection() as conn:
        version = await queries.get_latest_version(conn, site_id)
    if version is None:
        # Same 404 whether the site is unknown or simply has no successful generation yet.
        raise not_found("llms.txt")
    return version["content"]
