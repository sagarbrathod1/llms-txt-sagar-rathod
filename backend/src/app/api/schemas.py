"""Request and response models: the API contract.

The frontend's TypeScript types are generated from the OpenAPI schema built from these models
(frontend: npm run gen:api), so changing a model here changes the frontend types too.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Mirror the CHECK constraints in migrations/001_init.sql.
JobKind = Literal["generate", "recheck"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class CreateSiteRequest(BaseModel):
    # A plain string, not pydantic's HttpUrl: users type "example.com" without a scheme, and
    # the server normalizes it to an origin rather than rejecting it.
    url: str = Field(min_length=1, max_length=2048, examples=["example.com"])


class CreateSiteResponse(BaseModel):
    site_id: UUID
    # The new job, or the site's already queued/running job if there is one.
    job_id: UUID


class JobResponse(BaseModel):
    id: UUID
    site_id: UUID
    kind: JobKind
    status: JobStatus
    pages_discovered: int
    pages_crawled: int
    error: str | None
    warnings: list[str]  # non-fatal engine warnings, e.g. "robots.txt disallows /docs"
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class VersionSummary(BaseModel):
    id: UUID
    created_at: datetime


class VersionResponse(BaseModel):
    id: UUID
    site_id: UUID
    created_at: datetime
    content: str


class SiteResponse(BaseModel):
    id: UUID
    url: str
    monitor_enabled: bool
    check_interval_hours: int
    last_checked_at: datetime | None
    next_check_at: datetime | None
    # Failed checks in a row. Lets the UI explain why the worker turned monitoring off.
    consecutive_failures: int
    created_at: datetime
    # The site's most recent job, whatever its status, so the UI can show "generating..." after
    # a reload, or that the last recheck failed. None only before the first job exists.
    latest_job: JobResponse | None
    # None until the first job succeeds.
    latest_version: VersionResponse | None
    versions: list[VersionSummary]  # newest first


class UpdateSiteRequest(BaseModel):
    monitor_enabled: bool
