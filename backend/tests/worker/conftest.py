"""Shared setup for worker tests: async, against the real test database, with a fake engine."""

import pytest

from app import queries
from app.db import create_pool


@pytest.fixture
def anyio_backend():
    return "asyncio"  # psycopg's async API needs asyncio


@pytest.fixture
async def pool(db_url):
    async with create_pool() as pool:
        yield pool


@pytest.fixture
def claim(pool):
    """claim() takes the next queued job, as the loop does, and returns it (or None)."""

    async def claim_next() -> dict | None:
        async with pool.connection() as conn:
            return await queries.claim_job(conn)

    return claim_next
