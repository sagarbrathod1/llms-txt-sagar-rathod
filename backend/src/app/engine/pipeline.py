"""One generation run, as named steps: resolve the site, discover, crawl, select, render.

Takes a Fetcher rather than building one, so tests can run the whole pipeline against
httpx2.MockTransport and a fake DNS resolver.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from app.engine.crawl import Crawl, fetch_budget
from app.engine.discover import collect_sitemap_urls, fetch_robots
from app.engine.extract import Page, parse_page
from app.engine.fetch import (
    MAX_PAGE_BYTES,
    USER_AGENT,
    BlockedAddressError,
    Fetcher,
    FetchError,
    FetchResult,
)
from app.engine.layout import SiteLayout
from app.engine.models import GenerationError, GenerationResult, PageInfo, Progress
from app.engine.rank import unique_pages
from app.engine.render import render_llms_txt
from app.engine.titles import add_path_hints, choose_site_name, page_title, shared_title_parts
from app.engine.urls import normalize_page_url, origin_of

# A user watches a progress bar meanwhile, and ~2.5 minutes is the most we ask them to wait.
# Typical runs take 5-50 s, so this only bites on slow or throttled sites; the worker's
# 10-minute job limit stays as the backstop.
RUN_DEADLINE_SECONDS = 150
# A sitemap with fewer URLs than this is probably partial (e.g. only blog posts), so we
# follow links from the homepage instead.
MIN_SITEMAP_URLS = 10


@dataclass
class Site:
    """What we know about the site before crawling it: where it lives and its homepage."""

    origin: str  # after the homepage's redirects, e.g. https://www.example.com
    robots: RobotFileParser
    home: Page
    name: str
    warnings: list[str]


async def run(
    origin: str, on_progress: Callable[[Progress], None] | None, fetcher: Fetcher
) -> GenerationResult:
    fetcher.start_deadline(RUN_DEADLINE_SECONDS)
    site = await resolve_site(fetcher, origin)
    warnings = list(site.warnings)
    if delay := site.robots.crawl_delay(USER_AGENT):
        fetcher.set_crawl_delay(float(delay))
        warnings.append(
            f"robots.txt asks for {delay} s between requests, so pages are fetched one at a "
            "time and fewer of them fit in the time limit."
        )

    sitemap_urls, sitemap_warnings = await read_sitemaps(fetcher, site)
    warnings += sitemap_warnings
    layout = SiteLayout.detect(site.home, site.name, site.home.links + sitemap_urls)
    crawl = Crawl(
        fetcher, site.origin, site.robots, site.home, layout, on_progress, fetch_budget(fetcher)
    )
    crawl.discover(sitemap_urls)
    if len(set(sitemap_urls)) >= MIN_SITEMAP_URLS:
        # Homepage links too: the site's navigation is often missing from its sitemaps.
        await crawl.fetch_best(site.home.links + sitemap_urls)
    else:
        warnings.append(
            f"The sitemap listed fewer than {MIN_SITEMAP_URLS} pages (or there was none), "
            "so pages were also found by following links from the homepage."
        )
        # The homepage's links are level 1; any sitemap URLs join them so none are lost.
        await crawl.follow_links(site.home.links + sitemap_urls)
    crawl.raise_if_unusable()

    sections = crawl.ranker().build_sections(describe_pages(site, crawl))
    sections = add_path_hints(sections, layout.segments)
    summary = site.home.description or next(iter(site.home.paragraphs), None)
    if summary and _loose(summary) == _loose(site.name):
        summary = None  # "> Acme" under "# Acme" says nothing
    llms_txt = render_llms_txt(site.name, summary, sections, site.origin + "/")

    warnings += crawl.warnings()
    if not sections:
        warnings.append(
            "Found no pages besides the homepage. The site may build its links with "
            "JavaScript, which this tool doesn't run."
        )
    return GenerationResult(
        llms_txt=llms_txt,
        pages=[page for _, pages in sections for page in pages],
        pages_attempted=len(crawl.attempted),
        pages_failed=len(crawl.failed) + len(crawl.unsent),
        warnings=warnings,
    )


async def resolve_site(fetcher: Fetcher, origin: str) -> Site:
    """Read robots.txt and the homepage, following the homepage to where the site really is.

    example.com often redirects to www.example.com (or http to https). We crawl the site we
    landed on, since that's where its pages and links live, and obey that site's robots.txt.
    """
    warnings = []
    robots = await load_robots(fetcher, origin)
    home_result = await fetch_homepage(fetcher, origin)

    final_origin = origin_of(home_result.url)
    if final_origin is None:
        raise GenerationError(f"{origin} redirects to an address we can't crawl.")
    if final_origin != origin:
        warnings.append(f"{origin} redirects to {final_origin}, so that site was crawled.")
        origin = final_origin
        robots = await load_robots(fetcher, origin)

    home_url = normalize_page_url(home_result.url, home_result.url, origin) or origin + "/"
    home = await parse_page(home_result.body, home_url, origin, home_result.charset)
    hostname = urlsplit(origin).hostname or ""
    name = choose_site_name(home.site_name, home.alt_site_names, home.title, hostname)
    return Site(origin=origin, robots=robots, home=home, name=name, warnings=warnings)


async def load_robots(fetcher: Fetcher, origin: str) -> RobotFileParser:
    robots = await fetch_robots(fetcher, origin)
    if not robots.can_fetch(USER_AGENT, origin + "/"):
        raise GenerationError(
            f"{origin}/robots.txt doesn't allow crawlers like ours ({USER_AGENT}), "
            "so the site wasn't crawled."
        )
    return robots


async def fetch_homepage(fetcher: Fetcher, origin: str) -> FetchResult:
    """The homepage gives the site's name and summary, so without it there's no llms.txt."""
    try:
        return await fetcher.get(origin + "/", max_bytes=MAX_PAGE_BYTES, html_only=True)
    except BlockedAddressError:
        raise GenerationError(
            f"{origin} leads to a private or internal network address, so it can't be crawled."
        ) from None
    except FetchError as error:
        if error.status in (401, 403, 429):
            reason = f"it answered HTTP {error.status}; the site may block automated access"
        else:
            reason = str(error)
        raise GenerationError(f"Couldn't load the homepage of {origin}: {reason}.") from None


async def read_sitemaps(fetcher: Fetcher, site: Site) -> tuple[list[str], list[str]]:
    """Page URLs on this site from its sitemaps, and warnings about unreadable sitemaps.

    Sitemaps are the site's own list of pages and cost a few requests at most, so they're
    read before falling back to following links.
    """
    base = site.origin + "/"
    # Sitemap: lines should be absolute, but some sites write "/sitemap.xml".
    listed, bad = [], []
    for line in site.robots.site_maps() or []:
        try:
            listed.append(urljoin(base, line))
        except ValueError:  # e.g. "http://[bad": urljoin rejects malformed hosts
            bad.append(line)
    if listed:
        raw_urls, warnings = await collect_sitemap_urls(fetcher, listed)
    else:
        raw_urls, warnings = await collect_sitemap_urls(fetcher, [base + "sitemap.xml"], True)
    warnings += [f"robots.txt lists a malformed sitemap URL ({line!r}); skipped." for line in bad]
    urls = [url for raw in raw_urls if (url := normalize_page_url(raw, base, site.origin))]
    return urls, warnings


def describe_pages(site: Site, crawl: Crawl) -> list[PageInfo]:
    """One PageInfo per listable page, with a cleaned title and a page-specific description."""
    listable = unique_pages(list(crawl.pages.values()), site.home, crawl.layout.is_excluded)
    fetched = [site.home, *crawl.pages.values()]
    boilerplate = shared_title_parts([page.title for page in fetched if page.title])
    # Sections often carry boilerplate of their own ("| BBC Culture", "| BBC Travel") that is
    # too rare site-wide to count, so parts shared within a page's top folder are stripped too.
    folder_titles: dict[str, list[str]] = {}
    for page in fetched:
        if page.title and (folder := _top_folder(crawl.layout, page.url)):
            folder_titles.setdefault(folder, []).append(page.title)
    folder_boilerplate = {folder: shared_title_parts(t) for folder, t in folder_titles.items()}
    describe = _describer([site.home, *listable.values()])
    infos = []
    for url, page in listable.items():
        strip = boilerplate | folder_boilerplate.get(_top_folder(crawl.layout, url), set())
        title = page_title(page.title, site.name, url, strip)
        infos.append(PageInfo(url=url, title=title, description=describe(page, title), section=""))
    return infos  # sections are filled in by build_sections


def _top_folder(layout: SiteLayout, url: str) -> str | None:
    segments = layout.section_segments(url)
    return segments[0] if segments else None


def _describer(pages: list[Page]) -> Callable[[Page, str], str | None]:
    """Pick each page's description: its meta description, else its first paragraphs.

    Text that several pages share (the homepage's description copied site-wide by a CMS, an
    announcement banner) describes the site, not the page, so it's skipped in favor of the
    next choice.
    """
    counts = Counter(
        text for page in pages for text in {page.description, *page.paragraphs} if text
    )

    def describe(page: Page, title: str) -> str | None:
        for text in (page.description, *page.paragraphs):
            # A description that only repeats the title ("[About](...): About") adds nothing.
            if text and counts[text] == 1 and _loose(text) not in _loose(title):
                return text
        return None

    return describe


def _loose(text: str) -> str:
    """Lowercase letters and digits only, for "is this the same text" checks."""
    return "".join(char for char in text.lower() if char.isalnum())
