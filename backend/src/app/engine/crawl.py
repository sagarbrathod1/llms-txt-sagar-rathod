"""Fetch a site's pages within the page budget, and keep track of what happened to each."""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from urllib.robotparser import RobotFileParser

from app.engine.extract import Page, parse_page
from app.engine.fetch import MAX_PAGE_BYTES, USER_AGENT, DeadlineError, Fetcher, FetchError
from app.engine.layout import SiteLayout
from app.engine.models import GenerationError, Progress
from app.engine.rank import MAX_PAGES_PER_SECTION, Ranker, in_other_language, inlink_counts
from app.engine.titles import TITLE_SEPARATOR
from app.engine.urls import normalize_page_url

logger = logging.getLogger(__name__)

# Twice MAX_PAGES (50): headroom for pages that fail, are noindex, turn out to be duplicates
# via canonicals, or land in Optional. Also bounds how long one generation can take.
MAX_PAGES_FETCHED = 100
# Pages more clicks than this from the homepage are rarely the ones worth listing.
BFS_MAX_DEPTH = 3
# More failures than this, and more than half of the pages tried, mean the site is down or
# blocking us; a file built from the few that worked would misrepresent the site.
MIN_FAILURES_TO_GIVE_UP = 3
# Titles of the pages bot-protection services serve in place of the real page, often with
# HTTP 200 (Le Monde's "Client Challenge"). Matched against the title's first part without
# trailing punctuation, so "Just a moment..." and "Attention Required! | Cloudflare" match.
# Detected so we can stop and say so: we never try to get past them.
BOT_CHECK_TITLES = {
    "access denied", "are you a robot", "attention required", "client challenge",
    "ddos-guard", "human verification", "just a moment", "one more step",
    "pardon our interruption", "verifying you are human",
}  # fmt: skip


def is_bot_check(title: str | None) -> bool:
    if not title:
        return False
    first_part = TITLE_SEPARATOR.split(title)[0]
    return first_part.lower().rstrip(" .…!?") in BOT_CHECK_TITLES


def is_empty_shell(page: Page) -> bool:
    """No links into the site and no paragraph of text: nothing an llms.txt could describe.

    What a JavaScript app serves for every route before its scripts run, and what bot checks
    look like. Real pages, however short, link to the rest of their site.
    """
    return not page.links and not page.paragraphs


def fetch_budget(fetcher: Fetcher) -> int:
    """How many pages to fetch: MAX_PAGES_FETCHED, or fewer if they must be spaced out.

    With a Crawl-delay, only the pages that fit in the time left are planned, so the run
    lists its best pages instead of stopping at the deadline partway through.
    """
    if not fetcher.spacing:
        return MAX_PAGES_FETCHED
    return max(1, min(MAX_PAGES_FETCHED, int(fetcher.seconds_left() / fetcher.spacing)))


class Crawl:
    """One run's page fetches: what was fetched, what failed or was skipped, and progress."""

    def __init__(
        self,
        fetcher: Fetcher,
        origin: str,
        robots: RobotFileParser,
        home: Page,
        layout: SiteLayout,
        on_progress: Callable[[Progress], None] | None,
        max_pages: int = MAX_PAGES_FETCHED,
    ):
        self.fetcher = fetcher
        self.origin = origin
        self.robots = robots
        self.home = home
        self.layout = layout
        self.on_progress = on_progress
        self.max_pages = max_pages  # including the homepage
        self.out_of_time = False  # the run deadline stopped the crawl early
        self.attempted: set[str] = {home.url}
        self.discovered: set[str] = set()
        # Discovery order (homepage links, then sitemap order, then links found while
        # crawling), used to break ranking ties the way the site itself orders things.
        self.order: dict[str, int] = {}
        self.crawled = 1  # the homepage, fetched before the crawl starts
        self.planned = 1  # for progress: pages we've decided to fetch, homepage included
        self.pages: dict[str, Page] = {}  # by final URL, so redirects to one page count once
        # Transient failures (timeouts, 5xx) after a retry: these count as pages_failed, so
        # the worker can tell an outage from a real change.
        self.failed: dict[str, str] = {}
        # Planned but never sent: the run deadline came first. Counted as attempted and failed
        # (see GenerationResult), so a truncated recheck can't replace a complete version.
        self.unsent: set[str] = set()
        # 401/403: the site refused us. Also in `failed`: a recheck that was blocked must not
        # replace a good version. Tracked separately for the "blocking our crawler" error.
        self.refused: set[str] = set()
        # Pages answered with a bot check instead of the content (URL -> its title). Also in
        # `failed` and `refused`: the site refused us, just with HTTP 200.
        self.bot_checks: dict[str, str] = {}
        # Definite answers (404, not HTML, redirects off-site): the page simply isn't one we
        # can list. Not "failed", or a stale sitemap would block versioning forever.
        self.skipped: dict[str, str] = {}
        self.discover(home.links)

    def discover(self, urls: list[str]) -> None:
        for url in urls:
            self.order.setdefault(url, len(self.order))
        self.discovered.update(urls)

    def should_fetch(self, url: str) -> bool:
        return (
            url not in self.attempted
            and not self.layout.is_excluded(url)
            and self.robots.can_fetch(USER_AGENT, url)
        )

    def ranker(self, inlinks: dict[str, int] | None = None) -> Ranker:
        """A Ranker using inlinks counted over every page fetched so far (unless given)."""
        if inlinks is None:
            inlinks = inlink_counts([self.home, *self.pages.values()], self.layout.is_excluded)
        return Ranker(self.layout, inlinks, self.order, set(self.home.links))

    async def fetch_best(self, urls: list[str]) -> None:
        """Fetch the most promising of `urls` (from sitemaps) within the page budget.

        Homepage links count as inlinks here, since the homepage is the only page fetched so
        far. Spread evenly across sections first, so one big area can't use up the budget.
        """
        candidates = [url for url in dict.fromkeys(urls) if self.should_fetch(url)]
        candidates = self.layout.without_version_copies(candidates)
        ranker = self.ranker()
        budget = self.max_pages - len(self.attempted)
        await self._fetch_all(ranker.pick_spread(ranker.rank(candidates), budget))

    async def follow_links(self, start_urls: list[str]) -> None:
        """Breadth-first crawl over same-origin links, starting from start_urls as level 1.

        Used when sitemaps are missing or tiny. Each level is fetched concurrently; within a
        level, the most promising URLs go first in case the page budget runs out. "Promising"
        counts how many pages on the previous level linked to the URL. The per-section cap
        keeps a link trap (a calendar, faceted search) from eating the whole budget.
        """
        # How many pages we'll fetch isn't known until the last level, so progress reports the
        # budget meanwhile: the bar then only moves forward, and fills at the end.
        self.planned = self.max_pages
        frontier = Counter(start_urls)
        for _ in range(BFS_MAX_DEPTH):
            candidates = [url for url in frontier if self.should_fetch(url)]
            candidates = self.layout.without_version_copies(candidates)
            ranker = self.ranker(inlinks=frontier)
            budget = self.max_pages - len(self.attempted)
            batch = ranker.pick(ranker.rank(candidates), budget, 2 * MAX_PAGES_PER_SECTION)
            if not batch:
                break
            pages = await self._fetch_all(batch)
            frontier = Counter(link for page in pages for link in page.links)
        self.planned = len(self.attempted)
        self._report_progress()

    async def _fetch_all(self, urls: list[str]) -> list[Page]:
        self.planned = max(self.planned, len(self.attempted) + len(urls))
        self._report_progress()
        # gather keeps input order, and the Fetcher caps how many requests run at once.
        results = await asyncio.gather(*(self._fetch_page(url) for url in urls))
        pages = [page for page in results if page]
        # Recorded here, in input order, not as each fetch finishes: discovery order breaks
        # ranking ties, so it must not depend on which server response came back first.
        for page in pages:
            self.discover(page.links)
        return pages

    async def _fetch_page(self, url: str) -> Page | None:
        self.attempted.add(url)
        try:
            result = await self.fetcher.get(url, max_bytes=MAX_PAGE_BYTES, html_only=True)
        except DeadlineError:
            self.unsent.add(url)
            self.out_of_time = True
            self._count_crawled()  # done with it, so the progress bar still fills
            return None
        except FetchError as error:
            # Transient, rate-limited or refused: the page exists but we couldn't get it now.
            failed = error.retryable or error.status in (401, 403, 429)
            (self.failed if failed else self.skipped)[url] = str(error)
            if error.status in (401, 403):
                self.refused.add(url)
            self._count_crawled()
            return None
        self._count_crawled()

        final_url = normalize_page_url(result.url, result.url, self.origin)
        if final_url is None:
            self.skipped[url] = "redirects to another site"
            return None
        try:
            # Parsing is CPU work (tens of ms for a big page), so it runs on the parse thread.
            page = await parse_page(
                result.body, final_url, self.origin, result.charset,
                result.headers.get("x-robots-tag", ""),
            )  # fmt: skip
        # Deliberately broad: the HTML is hostile input, and a parser bug triggered by one page
        # must cost that page, not the run. Logged with the traceback so real bugs stay visible.
        except Exception:
            logger.warning("couldn't parse %s", url, exc_info=True)
            self.skipped[url] = "couldn't be parsed"
            return None
        if is_bot_check(page.title):
            self.bot_checks[url] = page.title or ""
            self.failed[url] = f"answered with a bot check, {page.title!r}"
            self.refused.add(url)
            return None
        self.pages.setdefault(final_url, page)
        return page

    def raise_if_unusable(self) -> None:
        """Stop with an error rather than publish a file built from a fraction of the site."""
        if self.fetcher.gave_up:
            raise GenerationError("This site is rate-limiting our crawler, so try again later.")
        tried = len(self.attempted) - len(self.unsent) - 1  # requests sent, minus the homepage
        if self._is_most_of(len(self.bot_checks), tried):
            counts = Counter(self.bot_checks.values())
            # Ties broken by the title itself: pages finish in any order, messages shouldn't.
            title = min(counts, key=lambda title: (-counts[title], title))
            raise bot_check_error(f"{len(self.bot_checks)} of the {tried} pages we tried", title)
        shell_title = self.shell_title()
        if shell_title and shell_title != self.home.title:
            # Identical empty pages under a title the homepage doesn't have: a bot check from
            # a service whose title we don't know. (Under the homepage's title: an app shell.)
            raise bot_check_error(f"most of the {len(self.pages)} pages we fetched", shell_title)
        if self._is_most_of(len(self.refused), tried):
            raise GenerationError(
                f"This site is blocking our crawler: {len(self.refused)} of the {tried} pages "
                "we tried answered HTTP 401 or 403, or with a bot check."
            )
        if self._is_most_of(len(self.failed), tried):
            raise GenerationError(
                f"{len(self.failed)} of the {tried} pages we tried couldn't be fetched "
                "(timeouts or server errors), so the result would be badly incomplete. "
                "Try again later."
            )

    @staticmethod
    def _is_most_of(count: int, tried: int) -> bool:
        return count >= MIN_FAILURES_TO_GIVE_UP and count > tried / 2

    def shell_title(self) -> str | None:
        """When most fetched pages are the same empty shell, its title ("" if untitled).

        A site whose pages can't be told apart can't be described page by page: it's either a
        JavaScript app (every route serves the homepage's shell) or a bot check.
        """
        titles = Counter(page.title or "" for page in self.pages.values() if is_empty_shell(page))
        if not titles:
            return None
        title, count = titles.most_common(1)[0]
        return title if self._is_most_of(count, len(self.pages)) else None

    def _count_crawled(self) -> None:
        self.crawled += 1
        self._report_progress()

    def _report_progress(self) -> None:
        if self.on_progress:
            self.on_progress(Progress(pages_discovered=self.planned, pages_crawled=self.crawled))

    def warnings(self) -> list[str]:
        """Aggregated, so a big site gives a few readable lines rather than hundreds."""
        out = []
        # Only an app shell gets this far: raise_if_unusable stops on a bot check's shell.
        if self.shell_title() is not None:
            out.append(
                "Most pages came back as the same empty page as the homepage: the site builds "
                "its pages with JavaScript, which this tool doesn't run. Their titles were taken "
                "from their URLs."
            )
        if self.out_of_time:
            out.append(
                "The crawl hit its time limit, so only the pages fetched by then are listed."
            )
        disallowed = sorted(
            url for url in self.discovered if not self.robots.can_fetch(USER_AGENT, url)
        )
        if disallowed:
            out.append(
                f"robots.txt disallows {len(disallowed)} discovered page(s), "
                f"e.g. {disallowed[0]}; they were left out."
            )
        other_language = sorted(
            url for url, page in self.pages.items() if in_other_language(page, self.home)
        )
        if other_language:
            out.append(
                f"{len(other_language)} page(s) are in another language than the homepage "
                f"and were left out, e.g. {other_language[0]}."
            )
        noindex = sorted(url for url, page in self.pages.items() if page.noindex)
        if noindex:
            out.append(
                f"{len(noindex)} page(s) are marked noindex and were left out, e.g. {noindex[0]}."
            )
        for problems, what in ((self.failed, "couldn't be fetched, even after a retry"),
                               (self.skipped, "were skipped")):  # fmt: skip
            if problems:
                url = min(problems)
                out.append(f"{len(problems)} page(s) {what}, e.g. {url} ({problems[url]}).")
        return out


def bot_check_error(what: str, title: str) -> GenerationError:
    return GenerationError(
        f'This site showed a bot check ("{title}") instead of {what}, so it can\'t be '
        "crawled. It blocks automated tools, and this one doesn't try to get past that."
    )
