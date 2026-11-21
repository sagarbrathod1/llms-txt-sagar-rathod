"""Turn raw <title> text into a site name and short, readable link titles."""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import unquote, urlsplit

from app.engine.models import PageInfo

# How titles join a page name and a site name: "Pricing | Acme", "Acme - Docs", "</> htmx ~ Docs",
# "Routing • SvelteKit Docs".
TITLE_SEPARATOR = re.compile(r"\s+[|\-–—·•:»~]+\s+")
# File extensions that say nothing in a title made from a URL ("intro.html" -> "Intro").
PAGE_EXTENSION = re.compile(r"\.(html?|php|aspx?|jsp)$", re.IGNORECASE)
# Path segments that are database IDs, not words: UUIDs, hex hashes (with at least one digit,
# so words like "facade" stay), and long numbers (/news/1030441/...). Four-digit numbers are
# kept: they're usually years, which make useful hints.
ID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}(-?[0-9a-f]{4}){3}-?[0-9a-f]{12}$|^(?=[a-f]*\d)[0-9a-f]{8,}$|^\d{5,}$",
    re.IGNORECASE,
)
# Homepage title parts that are never the site's name, and prefixes that aren't part of it.
GENERIC_TITLE_PARTS = {"home", "homepage", "home page", "index", "welcome"}
GENERIC_PREFIX = re.compile(r"^(welcome to|home of)\b\s*", re.IGNORECASE)
# A leading or trailing "Home" is dropped only when the rest is the domain's name ("BBC Home"
# on bbc.com): elsewhere it is often part of the brand ("Home Chef", "Zara Home").
HOME_PREFIX = re.compile(r"^(home ?page|home)\b\s*", re.IGNORECASE)
HOME_SUFFIX = re.compile(r"\s*\b(home ?page|home)$", re.IGNORECASE)
# A site name longer than this is a tagline ("Latest news, sport and opinion from the
# Guardian"); the hostname is the better H1 then.
MAX_SITE_NAME_WORDS = 5
# A title part found at the start or end of at least a quarter of the pages' titles is site
# or section boilerplate ("| MDN", "— Requests 2.34.2 documentation", "| Python Software
# Foundation"), not a page's name: page names are the parts that vary. Measured on real
# sites, suffixes like these appear on 30-60% of titles; page names on one or two.
SHARED_PART_SHARE = 0.25
# With fewer titles than this there's too little evidence to call any part boilerplate.
MIN_TITLES_FOR_SHARED_PARTS = 4
# ...and a part must also appear at least this many times.
MIN_SHARED_PART_COUNT = 3


def choose_site_name(
    site_name: str | None, alternatives: list[str], title: str | None, hostname: str
) -> str:
    """The H1: og:site_name, else application-name or JSON-LD name, else the homepage title.

    Candidates after og:site_name must be short, or they're taglines: the hostname is used.
    An og:site_name that is just the hostname ("fastht.ml") is skipped: the title usually
    names the site properly ("FastHTML"), and the hostname is the last resort anyway.
    """
    domain = hostname.removeprefix("www.")
    if site_name and site_name.lower().removeprefix("www.") != domain:
        return site_name
    for candidate in [*alternatives, site_name_from_title(title, hostname)]:
        if candidate and len(candidate.split()) <= MAX_SITE_NAME_WORDS:
            return candidate
    return domain


def site_name_from_title(title: str | None, hostname: str = "") -> str | None:
    """ "Acme | Widgets for everyone" -> "Acme": brand names are usually the shortest part.

    "Welcome to GOV.UK" -> "GOV.UK", "</> htmx" -> "htmx", and on bbc.com "BBC Home" -> "BBC".
    """
    if not title:
        return None
    host_label = hostname.removeprefix("www.").split(".")[0]
    parts = [
        _without_home(GENERIC_PREFIX.sub("", _strip_symbol_words(part)), host_label)
        for part in TITLE_SEPARATOR.split(title)
    ]
    parts = [part for part in parts if part and part.lower() not in GENERIC_TITLE_PARTS]
    return min(parts, key=len) if parts else None


def _without_home(name: str, host_label: str) -> str:
    """ "BBC Home" -> "BBC" on bbc.com; "Zara Home" and "Home Chef" stay as they are."""
    stripped = HOME_SUFFIX.sub("", HOME_PREFIX.sub("", name))
    return stripped if stripped and _alnum(stripped) == _alnum(host_label) else name


def _alnum(text: str) -> str:
    return "".join(char for char in text.lower() if char.isalnum())


def _strip_symbol_words(text: str) -> str:
    """Drop decorative words with no letters or digits from both ends ("</> htmx" -> "htmx").

    Only at the ends: a symbol inside a name is part of it ("Black & Decker").
    """
    words = text.split()
    while words and not any(char.isalnum() for char in words[0]):
        words.pop(0)
    while words and not any(char.isalnum() for char in words[-1]):
        words.pop()
    return " ".join(words)


def shared_title_parts(titles: list[str]) -> set[str]:
    """First and last title parts shared by many of the titles (compared loosely).

    Repeated until nothing new is shared: stripping "| Django" reveals the next layer,
    "| Django documentation".
    """
    if len(titles) < MIN_TITLES_FOR_SHARED_PARTS:
        return set()
    threshold = max(MIN_SHARED_PART_COUNT, len(titles) * SHARED_PART_SHARE)
    shared: set[str] = set()
    while True:
        counts: Counter[str] = Counter()
        for title in titles:
            parts = TITLE_SEPARATOR.split(_strip_ends(title, shared))
            if len(parts) > 1:
                counts.update({_comparable(parts[0]), _comparable(parts[-1])})
        new = {part for part, count in counts.items() if count >= threshold} - shared
        if not new:
            return shared
        shared |= new


def page_title(
    title: str | None,
    site_name: str,
    url: str,
    shared_parts: set[str] = frozenset(),
    home_title: str | None = None,
) -> str:
    """Drop the site name and shared boilerplate from both ends ("Pricing | Acme" -> "Pricing").

    Never empty: a page titled only with the site name, or with the homepage's whole title,
    falls back to its URL path. Shops title catch-all pages with the homepage's title
    (Patagonia: "Patagonia Outdoor Clothing & Gear"), and a JavaScript app's shell carries it
    on every route. A title that is only a shared part stays: "Python Software Foundation"
    is a fine name for the landing page of the section whose pages all end in it.
    """
    if title:
        says_nothing = {_comparable(site_name), _comparable(home_title or site_name)}
        title = _strip_symbol_words(_strip_ends(title, says_nothing | shared_parts))
        if title and _comparable(title) not in says_nothing:  # "</> Docs" -> "Docs" above
            return title
    return title_from_path(url)


def _strip_ends(title: str, strip: set[str]) -> str:
    """Remove title parts in `strip` from both ends, but never the last remaining part."""
    # A capturing split keeps the separators: ["Docs", " | ", "Acme"].
    tokens = re.split(f"({TITLE_SEPARATOR.pattern})", title)
    # Strip repeatedly: some CMSs append the name twice ("X - Acme - Acme").
    while len(tokens) > 1 and _comparable(tokens[-1]) in strip:
        tokens = tokens[:-2]
    while len(tokens) > 1 and _comparable(tokens[0]) in strip:
        tokens = tokens[2:]
    return "".join(tokens)


def title_from_path(url: str) -> str:
    """ "/docs/getting-started" -> "Getting started", for pages with no usable title.

    Codes at the end are skipped for the words before them: "/product/wolverine-way/BK212.html"
    -> "Wolverine way", "/careers/<uuid>" -> "Careers".
    """
    segments = [PAGE_EXTENSION.sub("", unquote(s)) for s in urlsplit(url).path.split("/") if s]
    if not segments:
        return "Home"
    named = [segment for segment in segments if not _is_code(segment)]
    return _readable(named[-1] if named else segments[-1]) or "Untitled"


def _is_code(segment: str) -> bool:
    """An ID, SKU or page number ("BK212", "2", a UUID) rather than words: no run of three
    letters, or an ID_SEGMENT."""
    return bool(ID_SEGMENT.match(segment)) or not re.search(r"[^\W\d_]{3}", segment)


def add_path_hints(
    sections: list[tuple[str, list[PageInfo]]], segments: Callable[[str], list[str]]
) -> list[tuple[str, list[PageInfo]]]:
    """Give titles that several listed pages share a hint from where their URLs differ.

    "Introduction" at /docs/2.x/intro and /docs/3.0.1/intro become "Introduction (2.x)" and
    "Introduction (3.0.1)", so an agent can tell the links apart.
    """
    pages = [page for _, section_pages in sections for page in section_pages]
    groups: dict[str, list[str]] = {}
    for page in pages:
        groups.setdefault(_comparable(page.title), []).append(page.url)
    hints: dict[str, str] = {}
    titles = {page.url: page.title for page in pages}
    for urls in groups.values():
        if len(urls) > 1:
            paths = {url: segments(url) for url in urls}
            for url in urls:
                hint = _distinguishing_segment(url, paths)
                # "Customer Requests (Customer requests)" would add nothing.
                if hint and _comparable(hint) != _comparable(titles[url]):
                    hints[url] = hint
    return [
        (name, [_with_hint(page, hints.get(page.url)) for page in section_pages])
        for name, section_pages in sections
    ]


def _distinguishing_segment(url: str, paths: dict[str, list[str]]) -> str | None:
    """The first path segment of `url` that differs from every other URL at that position.

    IDs are skipped: "(069c4628 88d7 4e4d ...)" tells a reader nothing. A later segment (a
    slug after a numeric ID) may still tell the pages apart; if none does, there's no hint.
    """
    mine = paths[url]
    others = [path for other, path in paths.items() if other != url]
    for index, segment in enumerate(mine):
        differs = all(len(path) <= index or path[index] != segment for path in others)
        if differs and not ID_SEGMENT.match(segment):
            return _readable(segment)
    return None


def _with_hint(page: PageInfo, hint: str | None) -> PageInfo:
    return replace(page, title=f"{page.title} ({hint})") if hint else page


def _readable(segment: str) -> str:
    """ "getting-started.html" -> "Getting started"."""
    words = PAGE_EXTENSION.sub("", unquote(segment)).replace("_", " ").replace("-", " ")
    return words[:1].upper() + words[1:]


def _comparable(text: str) -> str:
    """Case- and apostrophe-insensitive, since og:site_name and <title> often differ in both.

    Decorative symbols at the ends don't count either: "</> htmx" is the site name "htmx".
    """
    return _strip_symbol_words(text).lower().replace("’", "'")
