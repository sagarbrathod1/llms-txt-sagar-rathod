"""Shared fixtures for tests that need Postgres (tests/api, tests/worker, test_workflows.py).

They run against a real database, because the behavior under test lives in SQL: unique
indexes, ON CONFLICT, FOR UPDATE SKIP LOCKED. Point TEST_DATABASE_URL at a database that is
safe to wipe; every test starts from empty tables.
"""

import os

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import engine
from app.api.main import app
from app.config import settings
from app.db import run_migrations
from app.engine import GenerationResult, PageInfo

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://llms:llms@localhost:5432/llms_test"
)


@pytest.fixture(scope="session")
def migrated_db() -> str:
    # Idempotent, so a fresh CI database and a reused local one both end up current.
    run_migrations(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture
def db_url(migrated_db, monkeypatch) -> str:
    with psycopg.connect(migrated_db) as conn:
        conn.execute("TRUNCATE sites, jobs, llms_versions")
    # create_pool() reads settings.database_url, so the API and worker use the test database.
    monkeypatch.setattr(settings, "database_url", migrated_db)
    return migrated_db


class DbHelper:
    """Direct SQL, to set up or inspect state the code under test doesn't expose.

    Synchronous on purpose, even from async tests: each call is one short statement on its
    own connection, so blocking the event loop for it is harmless and keeps one helper.
    """

    def __init__(self, url: str):
        self.url = url

    def sql(self, query: str, params: tuple = ()) -> list[dict]:
        with psycopg.connect(self.url, row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    def add_site(self, url: str = "https://example.com", job_kind: str | None = None):
        """A site, optionally with a queued job. Returns (site_id, job_id)."""
        site_id = self.sql("INSERT INTO sites (url) VALUES (%s) RETURNING id", (url,))[0]["id"]
        return site_id, self.add_job(site_id, job_kind) if job_kind else None

    def add_job(self, site_id, kind: str = "generate"):
        """A queued job for an existing site. Returns its id."""
        return self.sql(
            "INSERT INTO jobs (site_id, kind) VALUES (%s, %s) RETURNING id", (site_id, kind)
        )[0]["id"]

    def job(self, job_id) -> dict:
        return self.sql("SELECT * FROM jobs WHERE id = %s", (job_id,))[0]

    def site(self, site_id) -> dict:
        return self.sql("SELECT * FROM sites WHERE id = %s", (site_id,))[0]

    def versions(self, site_id) -> list[str]:
        rows = self.sql(
            "SELECT content FROM llms_versions WHERE site_id = %s ORDER BY created_at", (site_id,)
        )
        return [row["content"] for row in rows]

    def make_due(self, site_id=None) -> None:
        """Move a site's (or every site's) next recheck into the past, as time passing would."""
        where, params = ("WHERE id = %s", (site_id,)) if site_id else ("", ())
        self.sql(f"UPDATE sites SET next_check_at = now() - interval '1 minute' {where}", params)


@pytest.fixture
def db(db_url) -> DbHelper:
    return DbHelper(db_url)


@pytest.fixture
def client(db_url):
    """The API, with its lifespan running (`with`), so its pool uses the test database."""
    with TestClient(app) as client:
        yield client


def engine_result(
    content: str = "# Example\n", pages: int = 10, failed: int = 0, attempted: int | None = None
) -> GenerationResult:
    """A fake engine result. By default every page attempted either failed or is in the file."""
    page = PageInfo(url="https://example.com/", title="Home", description=None, section="Docs")
    return GenerationResult(
        llms_txt=content,
        pages=[page] * pages,
        pages_attempted=pages + failed if attempted is None else attempted,
        pages_failed=failed,
    )


@pytest.fixture
def fake_engine(monkeypatch):
    """Replace the crawl engine, so API and worker tests need no network.

    fake_engine("# A\\n", failed=2) makes every run return that result; fake_engine(error=e)
    makes it raise e (any BaseException, so a leaked CancelledError can be faked too).
    """

    def set_outcome(content: str = "# Example\n", *, error: BaseException | None = None, **kw):
        result = engine_result(content, **kw)

        async def generate(url, on_progress=None):
            if error is not None:
                raise error
            return result

        monkeypatch.setattr(engine, "generate_llms_txt", generate)

    return set_outcome


@pytest.fixture
def make_result():
    """engine_result, for tests that build results without running the fake engine."""
    return engine_result
