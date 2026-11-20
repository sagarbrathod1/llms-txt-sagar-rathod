"""Find a site's pages: robots.txt, then sitemaps, then following links as a fallback."""

import zlib
from urllib.robotparser import RobotFileParser
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree
from defusedxml import DefusedXmlException

from app.engine.fetch import BlockedAddressError, DeadlineError, Fetcher, FetchError
from app.engine.layout import is_version, path_segments
from app.engine.models import GenerationError
from app.engine.parse_thread import run_in_parse_thread

# Google reads only the first 500 KiB of robots.txt; real files are a few KB.
MAX_ROBOTS_BYTES = 500_000
# Sitemap files fetched in total, index children included. Big sites split sitemaps by type
# (pages, posts, products); 20 files covers the main ones without a long discovery phase.
MAX_SITEMAPS = 20
# Index -> index -> urlset. The protocol forbids nested indexes, but some sites nest one level.
MAX_SITEMAP_DEPTH = 2
# We fetch at most ~100 pages, so more candidates than this add parsing time, not quality.
MAX_SITEMAP_URLS = 5000
# Per sitemap file, so one huge product or archive sitemap can't use up MAX_SITEMAP_URLS and
# starve the pages and blog sitemaps listed after it.
MAX_URLS_PER_SITEMAP = 1000
# The sitemap protocol allows up to 50 MB; 20 MB keeps memory bounded and covers real sites.
MAX_SITEMAP_BYTES = 20_000_000


async def fetch_robots(fetcher: Fetcher, origin: str) -> RobotFileParser:
    """Fetch and parse robots.txt, following RFC 9309 for errors.

    4xx (no robots.txt) means everything is allowed. A 5xx or no answer means "unreachable",
    which the RFC says to treat as disallowing everything, so we stop with an error.
    """
    parser = RobotFileParser()
    try:
        result = await fetcher.get(origin + "/robots.txt", max_bytes=MAX_ROBOTS_BYTES)
        lines = result.body.decode("utf-8", errors="replace").splitlines()
    except BlockedAddressError:
        raise GenerationError(
            "This address points to a private or internal network, so it can't be crawled."
        ) from None
    except FetchError as error:
        if error.status == 429:
            raise GenerationError(
                "This site is refusing automated requests (HTTP 429). Try again later."
            ) from None
        if error.retryable:
            raise GenerationError(
                f"Couldn't reach {origin} ({error}). Check the address, or try again later."
            ) from None
        lines = []  # 4xx, oversized or otherwise unusable: no rules, so everything is allowed
    # parse() also marks the parser as loaded; without it, can_fetch() returns False for all.
    parser.parse(lines)
    return parser


def parse_sitemap(data: bytes) -> tuple[list[str], list[str]]:
    """Return (page URLs, child sitemap URLs) from a <urlset> or a <sitemapindex>.

    Only <loc> elements directly inside <url>/<sitemap> are read: image and video sitemap
    extensions also use <loc> tags (e.g. <image:loc>), and those aren't pages.
    """
    root = defusedxml.ElementTree.fromstring(data)
    locs = [
        loc.text.strip()
        for entry in root
        for loc in entry
        if _local_name(loc.tag) == "loc" and loc.text and loc.text.strip()
    ]
    if _local_name(root.tag) == "sitemapindex":
        return [], locs
    return locs, []


def _local_name(tag: str) -> str:
    """'{http://www.sitemaps.org/schemas/sitemap/0.9}loc' -> 'loc'."""
    return tag.rsplit("}", 1)[-1]


async def collect_sitemap_urls(
    fetcher: Fetcher, sitemap_urls: list[str], guessed: bool = False
) -> tuple[list[str], list[str]]:
    """Read sitemaps breadth-first, following indexes, within the file and URL caps.

    Returns (page URLs in sitemap order, warnings about sitemaps that couldn't be used).
    `guessed` means the site didn't list sitemap_urls (we tried /sitemap.xml): then their
    being missing is normal, not worth a warning.
    """
    queue = [(url, 0) for url in sitemap_urls]
    seen: set[str] = set()
    pages: list[str] = []
    warnings: list[str] = []
    truncated = False
    while queue and len(seen) < MAX_SITEMAPS and len(pages) < MAX_SITEMAP_URLS:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            result = await fetcher.get(url, max_bytes=MAX_SITEMAP_BYTES)
            body = result.body
            # Sitemaps are often served as .xml.gz files, which the HTTP layer doesn't unpack.
            if body[:2] == b"\x1f\x8b":
                body = gunzip_capped(body, MAX_SITEMAP_BYTES)
            # Up to 20 MB of XML: CPU work, so it runs on the parse thread, off the event loop.
            page_urls, child_urls = await run_in_parse_thread(parse_sitemap, body)
        except DeadlineError:
            warnings.append("The time limit was reached while reading sitemaps.")
            break
        except FetchError as error:
            missing = error.status is not None and 400 <= error.status < 500
            if not (guessed and depth == 0 and missing):
                warnings.append(f"Couldn't read the sitemap {url} ({error}).")
            continue
        except (ParseError, DefusedXmlException):
            warnings.append(f"The sitemap {url} isn't valid sitemap XML; it was skipped.")
            continue
        if len(page_urls) > MAX_URLS_PER_SITEMAP:
            truncated = True
            # Docs sitemaps list every version, often old ones first (/docs/2.x/ sorts before
            # /docs/intro). When we can't read them all, version copies are what we leave out.
            current = [url for url in page_urls if not _has_version_folder(url)]
            page_urls = current + [url for url in page_urls if _has_version_folder(url)]
        pages.extend(page_urls[:MAX_URLS_PER_SITEMAP])
        if depth < MAX_SITEMAP_DEPTH:
            queue.extend((child, depth + 1) for child in child_urls)

    if truncated or len(pages) >= MAX_SITEMAP_URLS or any(url not in seen for url, _ in queue):
        warnings.append(
            f"The sitemaps are larger than we read ({MAX_SITEMAPS} files, "
            f"{MAX_URLS_PER_SITEMAP} URLs each, {MAX_SITEMAP_URLS} in all); "
            "pages beyond that weren't considered."
        )
    return pages[:MAX_SITEMAP_URLS], warnings


def gunzip_capped(data: bytes, max_bytes: int) -> bytes:
    """Decompress a .gz file (e.g. sitemap.xml.gz) without letting a gzip bomb fill memory."""
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)  # | 16: expect a gzip header
    try:
        out = decompressor.decompress(data, max_bytes)
    except zlib.error:
        raise FetchError("invalid gzip data") from None
    if decompressor.unconsumed_tail:
        raise FetchError("response too large")
    return out


def _has_version_folder(url: str) -> bool:
    return any(is_version(segment) for segment in path_segments(url))
