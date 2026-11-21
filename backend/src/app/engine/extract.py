"""Pull the metadata llms.txt needs out of one HTML page."""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

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
# Paragraphs examined per page before giving up: the description is near the top, and a
# page of thousands of tiny paragraphs mustn't cost thousands of structural checks.
MAX_PARAGRAPHS_EXAMINED = 50
# Paragraphs inside these are site chrome, not the page's content.
CHROME_TAGS = {"aside", "footer", "form", "header", "nav", "noscript"}
# class/id hints of banners, pop-ups and "JavaScript didn't run" notices (python.org's
# id="nojs") that sit inside the content area anyway.
CHROME_HINTS = re.compile(
    r"banner|consent|cookie|modal|newsletter|no-?js|popup|subscribe", re.IGNORECASE
)
HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
# Text that is nearly all link text is a list of links, not something that describes a page.
# A page like that is a hub: a section front, an archive, a list of essays, whose paragraphs
# introduce today's items, so they can't describe it (and would change daily). A paragraph like
# that is a tag cloud. Measured on real pages: hubs 0.80-0.99 (BBC and Spiegel section fronts,
# gov.uk's organisation list, htmx's essays); docs pages whose sidebar sits inside <main>
# 0.52-0.77 (svelte.dev's tutorial, hono.dev/examples); articles 0.02-0.45; tag clouds 0.9+;
# prose with inline links up to ~0.6 (jvns.ca's TILs).
LINK_LIST_SHARE = 0.8


@dataclass
class Page:
    url: str  # the URL we fetched, after redirects and normalization
    canonical: str | None  # same-origin <link rel="canonical">, normalized; None if absent
    title: str | None
    description: str | None  # from meta tags: description, og:description, twitter:description
    # The first few substantial paragraphs of the content: fallback descriptions, also used
    # when the meta description turns out to be one the whole site shares. Teasers for other
    # pages (a card's text under a link to the story) are left out: they aren't this page's.
    paragraphs: list[str]
    # Nearly all of the content's text is link text: a hub or listing page (see LINK_LIST_SHARE).
    is_hub: bool
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
    content = _content_root(soup)

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
        paragraphs=[_truncate(text) or text for text in _first_paragraphs(content, url, origin)],
        is_hub=_link_text_share(content) > LINK_LIST_SHARE,
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


def _content_root(soup: BeautifulSoup) -> Tag:
    """The page's content: <main> or <article> when the page marks it, else the whole body."""
    return soup.find("main") or soup.find("article") or soup.body or soup


def _first_paragraphs(root: Tag, url: str, origin: str) -> list[str]:
    """The first few substantial paragraphs of the page's own content, skipping site chrome,
    teasers for other pages, and paragraphs that are nearly all links (a tag cloud).

    One pass in document order, remembering the last heading seen, so the work stays linear
    in the page's size: the parse thread is shared, and nothing can interrupt it.
    """
    found: list[str] = []
    examined = 0
    heading: Tag | None = None  # the last heading before the current element
    for element in root.descendants:
        if not isinstance(element, Tag):
            continue
        if element.name in HEADINGS:
            # A heading in site chrome (a header's site title) doesn't introduce what follows.
            if not _inside_chrome(element, root):
                heading = element
            continue
        if element.name != "p":
            continue
        examined += 1
        if len(found) == MAX_PARAGRAPHS or examined > MAX_PARAGRAPHS_EXAMINED:
            break
        # Cheap checks first; the structural ones only run on real candidates.
        text = clean_text(element.get_text(" "))
        if not (text and len(text) >= MIN_PARAGRAPH_CHARS and _reads_as_description(text)):
            continue
        if (
            _inside_chrome(element, root)
            or _link_text_share(element) > LINK_LIST_SHARE
            or _is_teaser(element, heading, root, url, origin)
        ):
            continue
        found.append(text)
    return found


def _is_teaser(paragraph: Tag, heading: Tag | None, root: Tag, url: str, origin: str) -> bool:
    """Whether the paragraph introduces another page, as in a card on a section front.

    - A link to another page wraps it (BBC wraps whole cards).
    - The heading before it is a link to another page ("<h2><a href=/post>Title</a></h2>
      <p>First lines...</p>" on a blog's front page, or an off-site article on a tag page).
      Headings after it don't count ("Read next"), nor do headings that are mostly plain
      text ("By <a>Jo</a>"), link to the page itself (permalinks, "¶") or to the homepage
      (a site title in <h1><a href="/">).
    """
    for parent in paragraph.parents:
        if parent is root:
            break
        if parent.name == "a":
            return _links_elsewhere([parent], url, origin)
    if heading is None or _link_text_share(heading) <= LINK_LIST_SHARE:
        return False
    anchors = heading.find_all("a", href=True)
    if wrapper := heading.find_parent("a", href=True):
        anchors.append(wrapper)
    return _links_elsewhere(anchors, url, origin)


def _links_elsewhere(anchors: list[Tag], url: str, origin: str) -> bool:
    """Whether any of the links leads to another page: of this site, other than this page and
    the homepage, or of another site."""
    home = origin + "/"
    for anchor in anchors:
        href = anchor.get("href") or ""
        target = normalize_page_url(href, url, origin)
        if target is None:
            try:  # another site (http/https); mailto:, javascript: and files don't count
                if urlsplit(urljoin(url, href)).scheme in ("http", "https"):
                    return True
            except ValueError:  # malformed hosts ("http://[bad")
                pass
            continue
        if target.rstrip("/") not in (url.rstrip("/"), home.rstrip("/")):
            return True
    return False


def _link_text_share(root: Tag) -> float:
    """The share of the content's text (outside site chrome) that sits inside links."""
    total = linked = 0
    # A stack, not recursion: hostile HTML can nest deeper than Python's recursion limit.
    stack = [(root, False)]
    while stack:
        node, in_link = stack.pop()
        for child in node.children:
            if isinstance(child, Tag):
                if not _is_chrome(child):
                    stack.append((child, in_link or child.name == "a"))
            # Exactly NavigableString: its subclasses are comments, scripts and styles.
            elif type(child) is NavigableString:
                length = len(child.strip())
                total += length
                linked += length if in_link else 0
    return linked / total if total else 0.0


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
