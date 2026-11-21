"""One generation run, as named steps: resolve the site, discover, crawl, select, render.

Takes a Fetcher rather than building one, so tests can run the whole pipeline against
httpx2.MockTransport and a fake DNS resolver.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from app.engine.crawl import (
    REFUSAL_STATUSES,
    Crawl,
    bot_check_error,
    fetch_budget,
    is_bot_check,
    is_empty_shell,
)
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
from app.engine.layout import SiteLayout, is_locale, is_version, path_segments
from app.engine.models import GenerationError, GenerationResult, PageInfo, Progress
from app.engine.rank import unique_pages
from app.engine.render import render_llms_txt
from app.engine.titles import (
    MAX_SITE_NAME_WORDS,
    TITLE_SEPARATOR,
    add_path_hints,
    choose_site_name,
    page_title,
    shared_title_parts,
    title_from_path,
)
from app.engine.urls import normalize_page_url, origin_of

# A user watches a progress bar meanwhile, and ~2.5 minutes is the most we ask them to wait.
# Typical runs take 5-50 s, so this only bites on slow or throttled sites; the worker's
# 10-minute job limit stays as the backstop.
RUN_DEADLINE_SECONDS = 150
# A sitemap with fewer URLs than this is probably partial (e.g. only blog posts), so we
# follow links from the homepage instead.
MIN_SITEMAP_URLS = 10
# A page linking to at least this many dated or numbered items is a feed (see _is_feed).
FEED_MIN_ITEMS = 25


@dataclass
class Site:
    """What we know about the site before crawling it: where it lives and its homepage."""

    origin: str  # after the homepage's redirects, e.g. https://www.example.com
    # The folder the site lives in: "/" unless the homepage redirected into a folder of
    # another site (docs.pydantic.dev -> pydantic.dev/docs/), when only that folder is crawled.
    base: str
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
        # Say what we did: a long delay is capped (see MAX_CRAWL_DELAY_SECONDS).
        spacing = f"{fetcher.spacing:g} s apart"
        if fetcher.spacing < float(delay):
            spacing += " (our maximum)"
        warnings.append(
            f"robots.txt asks for {delay} s between requests, so pages were fetched one at a "
            f"time, {spacing}, and fewer of them fit in the time limit."
        )

    sitemap_urls, sitemap_warnings = await read_sitemaps(fetcher, site)
    warnings += sitemap_warnings
    layout = SiteLayout.detect(site.home, site.name, site.home.links + sitemap_urls, site.base)
    crawl = Crawl(
        fetcher, site.origin, site.robots, site.home, layout, on_progress, fetch_budget(fetcher)
    )
    crawl.discover(sitemap_urls)
    # Only URLs the crawl may fetch count: a scoped crawl (pydantic.dev/docs/) can't use the
    # root sitemap's marketing pages, and other docs versions are copies.
    if len({url for url in sitemap_urls if not layout.is_excluded(url)}) >= MIN_SITEMAP_URLS:
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
    summary = site.home.description or next(iter(_own_paragraphs(site.home, layout)), None)
    if summary and _loose(summary) == _loose(site.name):
        summary = None  # "> Acme" under "# Acme" says nothing
    llms_txt = render_llms_txt(site.name, summary, sections, site.origin + site.base)

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
    When a site redirects into a folder of another one (docs.pydantic.dev ->
    pydantic.dev/docs/), that folder is what the user asked about, so the crawl stays in it.
    """
    warnings = []
    robots = await load_robots(fetcher, origin)
    home_result = await fetch_homepage(fetcher, origin)

    final_origin = origin_of(home_result.url)
    if final_origin is None:
        raise GenerationError(f"{origin} redirects to an address we can't crawl.")
    requested = origin
    if final_origin != origin:
        origin = final_origin
        robots = await load_robots(fetcher, origin)

    home_url = normalize_page_url(home_result.url, home_result.url, origin) or origin + "/"
    home = await parse_page(home_result.body, home_url, origin, home_result.charset)
    if is_bot_check(home):
        raise bot_check_error("its homepage", home.title or "")
    base = "/"
    if origin != requested:
        if _host(origin) != _host(requested):
            base = _section_folder(home)
        moved_to = origin + base.rstrip("/")
        part = "that part of the site" if base != "/" else "that site"
        warnings.append(f"{requested} redirects to {moved_to}, so {part} was crawled.")
    hostname = urlsplit(origin).hostname or ""
    name = choose_site_name(home.site_name, home.alt_site_names, home.title, hostname)
    return Site(origin=origin, base=base, robots=robots, home=home, name=name, warnings=warnings)


def _host(origin: str) -> str:
    """ "https://www.example.com" -> "example.com": www. and http variants are one site."""
    return (urlsplit(origin).hostname or "").removeprefix("www.")


def _section_folder(home: Page) -> str:
    """The top folder another site's redirect landed in, if the site we asked about lives there.

    /docs, /docs/ and /docs/getting-started/overview all give "/docs/" (locale and version
    folders in front are kept: "/en/docs/"), but only if the landing page links to other
    pages under it: a docs section does, while a lone landing page like /welcome doesn't, and
    the whole site is crawled then. Deeper folders are left to SiteLayout's version scoping.
    """
    kept: list[str] = []
    for segment in path_segments(home.url):
        kept.append(segment)
        if not (is_locale(segment) or is_version(segment)):
            folder = "/" + "".join(part + "/" for part in kept)
            under = (link for link in home.links if link != home.url)
            if any("/".join(path_segments(link)).startswith(folder[1:]) for link in under):
                return folder
            break
    return "/"


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
        if error.status in (*REFUSAL_STATUSES, 429):
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
            url = urljoin(base, line)
        except ValueError:  # e.g. "http://[bad": urljoin rejects malformed hosts
            bad.append(line)
            continue
        # Some sites list their llms.txt or a plain-text sitemap here. We read XML sitemaps
        # only, and warning that a text file "isn't valid sitemap XML" would just confuse.
        if not urlsplit(url).path.lower().endswith(".txt"):
            listed.append(url)
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
    describe = _describer([site.home, *listable.values()], crawl.layout)
    shell_title = crawl.shell_title()  # most pages are one empty shell: its title names none
    infos = []
    for url, page in listable.items():
        strip = boilerplate | folder_boilerplate.get(_top_folder(crawl.layout, url), set())
        if shell_title is not None and is_empty_shell(page) and (page.title or "") == shell_title:
            title = title_from_path(url)
        else:
            title = page_title(page.title, site.name, url, strip, site.home.title)
        infos.append(PageInfo(url=url, title=title, description=describe(page, title), section=""))
    return infos  # sections are filled in by build_sections


def _top_folder(layout: SiteLayout, url: str) -> str | None:
    segments = layout.section_segments(url)
    return segments[0] if segments else None


def _own_paragraphs(page: Page, layout: SiteLayout) -> list[str]:
    """Paragraphs that may describe the page: none on a hub (nearly all links) or a feed.

    Their paragraphs introduce the latest items. Their meta description, if any, is stable;
    their paragraphs aren't, and a description that changed daily would make monitoring
    store a new version every day.
    """
    return [] if page.is_hub or _is_feed(page, layout) else page.paragraphs


def _is_feed(page: Page, layout: SiteLayout) -> bool:
    """A page listing many dated or numbered items: a blog's front page, a news section.

    Measured: such listings link to 37-599 items (The Verge's sections, simonwillison.net,
    jvns.ca, BBC Arts); docs pages to 0-1. A page that is itself an item is never a feed,
    however many items its sidebar lists: ordinary articles link to anywhere from 3 to
    hundreds (krebsonsecurity.com 27, a kubernetes.io blog post 777, Google Help 108).
    """
    if layout.is_item(page.url):
        return False
    return sum(layout.is_item(link) for link in page.links) >= FEED_MIN_ITEMS


def _describer(pages: list[Page], layout: SiteLayout) -> Callable[[Page, str], str | None]:
    """Pick each page's description: its meta description, else its first paragraphs.

    Text that several pages share (the homepage's description copied site-wide by a CMS, an
    announcement banner) describes the site, not the page, so it's skipped in favor of the
    next choice.
    """
    counts = Counter(
        text
        for page in pages
        for text in {page.description, *_own_paragraphs(page, layout)}
        if text
    )

    def describe(page: Page, title: str) -> str | None:
        for text in (page.description, *_own_paragraphs(page, layout)):
            if text and counts[text] == 1 and not _echoes(text, title):
                return text
        return None

    return describe


def _echoes(description: str, title: str) -> bool:
    """A description that only repeats the title adds nothing: "[About](...): About", or
    svelte.dev's "[Routing](...): Routing • SvelteKit documentation", the title plus a label.

    A longer remainder is real content ("Sign in to your account - report a change, ...").
    """
    if _loose(description) in _loose(title):
        return True
    first_part, *rest = TITLE_SEPARATOR.split(description, maxsplit=1)
    label = rest[0] if rest else ""
    return _loose(first_part) == _loose(title) and len(label.split()) <= MAX_SITE_NAME_WORDS


def _loose(text: str) -> str:
    """Lowercase letters and digits only, for "is this the same text" checks."""
    return "".join(char for char in text.lower() if char.isalnum())
