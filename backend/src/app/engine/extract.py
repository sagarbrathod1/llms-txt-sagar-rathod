"""Pull the metadata llms.txt needs out of one HTML page."""

import json
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from app.engine.parse_thread import run_in_parse_thread
from app.engine.urls import normalize_page_url

logger = logging.getLogger(__name__)
# bs4 logs a WARNING for every page with undecodable bytes, which without logging config
# goes straight to stderr. It's routine on real sites (mislabelled charsets); we record it
# at debug level below instead.
logging.getLogger("bs4.dammit").setLevel(logging.ERROR)

# Some sites stuff whole paragraphs into meta descriptions. A list item should stay one
# readable line, and search engines show roughly this much anyway.
MAX_DESCRIPTION_CHARS = 300
# A fallback description taken from the page text must be a real sentence or two, not a
# button label or a date line.
MIN_PARAGRAPH_CHARS = 50
# A fallback paragraph must read like a description: a finished sentence, with no date or
# version number in it. Listing pages ("Posted Nov 19 by ...", "Download v22.11.0") change
# with every new item, and the monitored llms.txt would churn with them.
SENTENCE_END = re.compile(r"[.!?。！？…][\"'”’)\]]*$")
DATE_OR_VERSION = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}\b"
    r"|\b\d{1,2} (jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b"
    r"|\b(19|20)\d{2}-\d{2}-\d{2}\b"
    r"|\bv?\d+\.\d+\.\d+\b",
    re.IGNORECASE,
)
# HTML that some CMSs leave inside meta descriptions (<p>...</p>).
TAG = re.compile(r"<[^>]+>")
# Paragraphs kept per page. The first is often a site-wide banner ("Try our new ...") that
# every page shares; the next ones give the description a chance to be about the page.
MAX_PARAGRAPHS = 3
# Paragraphs inside these are site chrome, not the page's content.
CHROME_TAGS = {"aside", "footer", "form", "header", "nav", "noscript"}
# class/id hints of banners, pop-ups and "JavaScript didn't run" notices (python.org's
# id="nojs") that sit inside the content area anyway.
CHROME_HINTS = re.compile(
    r"banner|consent|cookie|modal|newsletter|no-?js|popup|subscribe", re.IGNORECASE
)


@dataclass
class Page:
    url: str  # the URL we fetched, after redirects and normalization
    canonical: str | None  # same-origin <link rel="canonical">, normalized; None if absent
    title: str | None
    description: str | None  # from meta tags: description, og:description, twitter:description
    # The first few substantial paragraphs of the content: fallback descriptions, also used
    # when the meta description turns out to be one the whole site shares.
    paragraphs: list[str]
    site_name: str | None  # og:site_name; only used from the homepage
    # Other names the homepage gives the site: application-name, then JSON-LD WebSite or
    # Organization name. Used for the H1 when there's no og:site_name.
    alt_site_names: list[str]
    # Primary language subtag of <html lang>, e.g. "en" for "en-GB"; None if absent.
    lang: str | None
    noindex: bool
    links: list[str]  # same-origin page links, normalized, deduped, in document order
    # Same-origin URLs of this page in other languages (<link rel="alternate" hreflang>).
    alternates: list[str]


def clean_text(text: str | None) -> str | None:
    """Collapse whitespace (including newlines, which would break markdown lines)."""
    if text is None:
        return None
    return " ".join(text.split()) or None


async def parse_page(
    body: bytes, url: str, origin: str, charset: str | None = None, x_robots_tag: str = ""
) -> Page:
    """extract_page, run on the parse thread so the event loop keeps serving meanwhile."""
    return await run_in_parse_thread(extract_page, body, url, origin, charset, x_robots_tag)


def extract_page(
    body: bytes, url: str, origin: str, charset: str | None = None, x_robots_tag: str = ""
) -> Page:
    """Parse one HTML page. `url` is its final URL; links are resolved against it.

    Title: <title>, else og:title, else the first <h1>. Description: meta description, else
    og:description, else twitter:description; the first real paragraphs are kept separately.
    noindex comes from <meta name="robots"> or the X-Robots-Tag header. Attribute values are
    matched case-insensitively (<META NAME="Description">, rel="Canonical").
    """
    # Passing bytes lets BeautifulSoup honor <meta charset> when the header names no charset.
    # html.parser (stdlib) over lxml: no compiled dependency, and fast enough for ~100 pages.
    soup = BeautifulSoup(body, "html.parser", from_encoding=charset)
    if soup.contains_replacement_characters:
        logger.debug("%s: some characters couldn't be decoded", url)
    meta = _meta_tags(soup)

    title = _title_tag_text(soup) or meta.get("og:title")
    if not title and (h1 := soup.find("h1")):
        title = clean_text(h1.get_text(" "))

    description = (
        meta.get("description") or meta.get("og:description") or meta.get("twitter:description")
    )
    noindex = "noindex" in (meta.get("robots", "") + "," + x_robots_tag).lower()

    canonical = None
    for tag in _links_with_rel(soup, "canonical")[:1]:
        canonical = normalize_page_url(tag["href"], url, origin)

    alternates = []
    for tag in _links_with_rel(soup, "alternate"):
        hreflang = (tag.get("hreflang") or "").lower()
        if hreflang and hreflang != "x-default":  # x-default is the language picker
            if alternate := normalize_page_url(tag["href"], url, origin):
                alternates.append(alternate)

    links: list[str] = []
    seen: set[str] = set()
    # <area> too: some sites navigate with image maps (paulgraham.com's essay index).
    for anchor in soup.find_all(["a", "area"], href=True):
        link = normalize_page_url(anchor["href"], url, origin)
        if link and link not in seen:
            seen.add(link)
            links.append(link)

    return Page(
        url=url,
        canonical=canonical,
        title=title,
        description=_truncate(description),
        paragraphs=[_truncate(text) or text for text in _first_paragraphs(soup)],
        site_name=meta.get("og:site_name"),
        alt_site_names=[
            name for name in [meta.get("application-name"), *_jsonld_names(soup)] if name
        ],  # fmt: skip
        lang=_language(soup),
        noindex=noindex,
        links=links,
        alternates=alternates,
    )


def _meta_tags(soup: BeautifulSoup) -> dict[str, str]:
    """<meta name|property=... content=...> as {lowercased name: content}; first one wins."""
    found: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or "").strip().lower()
        content = clean_text(TAG.sub(" ", tag.get("content") or ""))
        if key and content:
            found.setdefault(key, content)
    return found


def _language(soup: BeautifulSoup) -> str | None:
    html = soup.find("html")
    lang = (html.get("lang") or "") if html else ""
    return lang.strip().lower().replace("_", "-").split("-")[0] or None


def _title_tag_text(soup: BeautifulSoup) -> str | None:
    """The document <title>, not the <title> of an inline <svg> icon placed before it."""
    for tag in soup.find_all("title"):
        if tag.find_parent("svg") is None:
            return clean_text(tag.get_text(" "))
    return None


def _links_with_rel(soup: BeautifulSoup, rel: str) -> list[Tag]:
    """<link> tags whose rel includes `rel`, ignoring case, that have an href."""
    return [
        tag
        for tag in soup.find_all("link", href=True)
        # BeautifulSoup splits rel into a list of values, since it may hold several.
        if rel in (value.lower() for value in tag.get("rel") or [])
    ]


def _first_paragraphs(soup: BeautifulSoup) -> list[str]:
    """The first few substantial paragraphs of the page's content, skipping site chrome.

    Looks in <main> or <article> when the page marks its content, else the whole body.
    """
    root = soup.find("main") or soup.find("article") or soup.body or soup
    found: list[str] = []
    for paragraph in root.find_all("p"):
        if len(found) == MAX_PARAGRAPHS:
            break
        if _inside_chrome(paragraph, root):
            continue
        text = clean_text(paragraph.get_text(" "))
        if text and len(text) >= MIN_PARAGRAPH_CHARS and _reads_as_description(text):
            found.append(text)
    return found


def _reads_as_description(text: str) -> bool:
    return bool(SENTENCE_END.search(text)) and not DATE_OR_VERSION.search(text)


def _jsonld_names(soup: BeautifulSoup) -> list[str]:
    """Names of the site from JSON-LD: WebSite entries first, then Organization."""
    found: dict[str, list[str]] = {"WebSite": [], "Organization": []}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:  # broken JSON-LD is common; it's only a fallback
            continue
        items = data if isinstance(data, list) else [data]
        items += [item for d in items if isinstance(d, dict) for item in d.get("@graph", [])]
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            types = item.get("@type")
            for kind in found:
                if kind == types or (isinstance(types, list) and kind in types):
                    found[kind].append(clean_text(item["name"]) or "")
    return [name for name in found["WebSite"] + found["Organization"] if name]


def _inside_chrome(paragraph: Tag, root: Tag) -> bool:
    """Whether a container between the paragraph and the content root is site chrome.

    Ancestors above the root don't count: many sites put class="no-js" on <html> itself.
    """
    for parent in paragraph.parents:
        if parent is root:
            return False
        if _is_chrome(parent):
            return True
    return False


def _is_chrome(tag: Tag) -> bool:
    if tag.name in CHROME_TAGS:
        return True
    hints = " ".join([*(tag.get("class") or []), tag.get("id") or ""])
    return bool(CHROME_HINTS.search(hints))


def _truncate(text: str | None) -> str | None:
    if text is None or len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    # Cut at a word boundary so the line doesn't end mid-word.
    return text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"
