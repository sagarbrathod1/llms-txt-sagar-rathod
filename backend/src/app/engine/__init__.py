"""The crawl-and-generate engine: URL in, llms.txt out.

Pure library: no database, no FastAPI. The worker depends only on generate_llms_txt, so the
engine can be tested and run from a CLI on its own.
"""

from collections.abc import Callable

import httpx2

from app.engine import pipeline
from app.engine.fetch import TIMEOUT_SECONDS, Fetcher
from app.engine.models import GenerationError, GenerationResult, PageInfo, Progress

__all__ = ["GenerationError", "GenerationResult", "PageInfo", "Progress", "generate_llms_txt"]


async def generate_llms_txt(
    url: str, on_progress: Callable[[Progress], None] | None = None
) -> GenerationResult:
    """Crawl the site at `url` and build its llms.txt.

    on_progress is a plain sync callback, called as pages are discovered and crawled. It only
    records the latest Progress; the worker flushes that to the database from its own timer.
    So the engine never awaits I/O for progress and never knows about the database.

    Output is deterministic: the same site content gives byte-identical llms_txt, with stable
    ordering of sections and pages. Version comparison hashes the output, so any
    nondeterminism would store a spurious new version on every recheck.

    Raises GenerationError, with a user-safe message, for expected failures. Any other
    exception is a bug.
    """
    # follow_redirects=False: the Fetcher follows them itself so every hop gets the SSRF check.
    async with httpx2.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        return await pipeline.run(url, on_progress, Fetcher(client))
