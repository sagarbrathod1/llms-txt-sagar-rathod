"""Data passed in and out of the engine.

Plain dataclasses, not pydantic: the engine is a pure library with no web or DB dependencies.
"""

from dataclasses import dataclass, field


class GenerationError(Exception):
    """An expected failure (site unreachable, blocked by robots.txt, no pages found, ...).

    Its message is shown to users as-is, so it must be safe and readable: no stack traces,
    internal hostnames or raw exception text. Any other exception raised by the engine is a
    bug, and the worker shows a generic message for it instead.
    """


@dataclass
class Progress:
    """How far a run is, for a progress bar: pages_crawled out of pages_discovered.

    pages_discovered is the number of pages the engine has decided to fetch (the homepage
    included), not every URL it has seen: a sitemap can list thousands, of which at most
    ~100 are fetched, and a "37 of 5000" bar would never fill. While following links, the
    final count isn't known, so it reports the page budget, then the exact count at the end.
    pages_crawled counts pages finished, successful or not (including ones the time limit
    cut off). So pages_crawled / pages_discovered never goes backwards and ends at 1;
    pages_discovered itself can drop once, at the end of a link-following crawl.
    """

    pages_discovered: int
    pages_crawled: int


@dataclass
class PageInfo:
    url: str
    title: str  # never empty: the engine falls back to something (e.g. the URL path)
    description: str | None  # many pages have no meta description
    section: str  # the llms.txt H2 section this page was placed under


@dataclass
class GenerationResult:
    llms_txt: str
    # The pages listed in llms_txt, in file order. Usually fewer than were fetched: the engine
    # fetches extra pages and keeps the best.
    pages: list[PageInfo]
    # Every page we planned to fetch, homepage included: fetched, skipped (404, not HTML),
    # failed, or never sent because the run hit its time limit. The denominator for
    # pages_failed.
    pages_attempted: int
    # Pages we planned but couldn't get: failed even after a retry (timeouts, connection
    # errors, 5xx, 429), refused (401, 403), or cut off by the run's time limit. A 404 isn't a
    # failure: the page doesn't exist. On a recheck the worker skips versioning when
    # pages_failed / pages_attempted is too high, so an outage, a block or a truncated run
    # doesn't replace a complete version.
    pages_failed: int
    # Non-fatal problems worth showing the user (e.g. "robots.txt disallows /docs").
    warnings: list[str] = field(default_factory=list)
