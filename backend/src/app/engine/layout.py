"""What a site's URL layout tells us: where its content lives, and which URLs are copies.

Many sites serve the same pages several times: per language (/de/, /fr-fr/), per docs
version (/docs/2.x/, /en/stable/), or as build artifacts (/_modules/, /genindex). Listing
those copies would crowd the real pages out of llms.txt, so this module identifies them.
"""

import re
from collections import Counter
from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, unquote, urlsplit

from app.engine.extract import Page

# ISO 639-1 language codes. A real list rather than "any two letters", which would also
# match folders like /ai/, /go/ or /tv/.
ISO_639_1 = set(
    "aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu "
    "cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi "
    "ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks "
    "ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl "
    "nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl "
    "sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve "
    "vi vo wa wo xh yi yo za zh zu".split()
)
# A language code with an optional region or script: en, en-us, pt_BR, zh-hans, es-419.
LOCALE = re.compile(r"^([a-z]{2})(?:[-_]([a-z]{2,4}|\d{3}))?$")
# A version folder: 2.x, 3.0.1, v2, 2.0.0-alpha.30.
VERSION = re.compile(r"^v?\d+(\.[\w-]+)+$|^v\d+$")
# Names docs sites give their moving versions. Each is a whole copy of the docs.
VERSION_ALIASES = {"latest", "stable", "next", "current", "nightly", "canary", "unreleased"}
# Build output of docs generators (Sphinx's _modules, _sources, genindex) and search pages:
# never something to point an agent at.
JUNK_SEGMENTS = {"_images", "_modules", "_sources", "_static", "genindex", "py-modindex",
                 "search", "searchindex"}  # fmt: skip
# Query parameters that mean "go here after logging in": the URL is a login page.
REDIRECT_PARAMS = {"next", "redirect", "redirect_to", "redirect_uri", "return", "return_to",
                   "returnto", "returnurl"}  # fmt: skip
# Dated content (news, posts) changes daily; listing it would make the monitored llms.txt
# churn even when nothing that matters changed. /2026/11/... or a date-stamped slug.
# When one folder holds at least this share of a site's pages (MDN's /docs/, Docusaurus's
# /docs/), grouping by it would give one giant section, so we group one level deeper.
DOMINANT_FOLDER_SHARE = 0.6
# ...judged only with enough URLs to go on.
MIN_URLS_FOR_DEEPER_GROUPING = 10
# Archives are never grouped deeper: their subfolders are years or authors, not topics, and
# a site whose pages are mostly posts should list a few, not dozens (nodejs.org's blog).
ARCHIVE_FOLDERS = {"articles", "blog", "changelog", "news", "posts", "press", "releases"}
YEAR = re.compile(r"^(19|20)\d{2}$")
MONTH = re.compile(
    r"^(0?[1-9]|1[0-2]|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february"
    r"|march|april|june|july|august|september|october|november|december)$"
)  # /2026/11/... and /2026/nov/... (the Guardian)
DATE_IN_SLUG = re.compile(r"(19|20)\d{2}[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])")


def is_locale(segment: str) -> bool:
    match = LOCALE.match(segment.lower())
    return bool(match) and match.group(1) in ISO_639_1


def is_version(segment: str) -> bool:
    return bool(VERSION.match(segment)) or segment.lower() in VERSION_ALIASES


def path_segments(url: str) -> list[str]:
    """Decoded, lowercased, non-empty path segments: "/Docs/a%20b/" -> ["docs", "a b"]."""
    return [unquote(segment).lower() for segment in urlsplit(url).path.split("/") if segment]


@dataclass(frozen=True)
class SiteLayout:
    # The homepage's locale/version folders, e.g. "/en/latest/" or "/en-us/" (lowercase).
    # Stripped from paths before naming sections.
    scope: str = "/"
    # True when the scope includes a version folder: then only pages under it are listed,
    # since every other version tree is a copy. A locale-only scope isn't strict: sites like
    # nodejs.org (/en) and twilio.com (/en-us) keep /learn or /docs outside it.
    strict_scope: bool = False
    home_locale: str | None = None  # the scope's locale folder, e.g. "en-us"
    other_locales: frozenset[str] = frozenset()  # folders of translations, from hreflang
    brand_words: frozenset[str] = frozenset()  # e.g. {"npr"}: "/about-npr/" -> "About"
    # A top folder holding most of the site (MDN's "docs"): its pages are grouped one level
    # deeper, by /docs/<folder>/, so it doesn't become one giant section.
    dominant_folder: str | None = None

    @classmethod
    def detect(cls, home: Page, site_name: str, urls: list[str], base: str = "/") -> "SiteLayout":
        """Work out the layout from the homepage (after redirects) and the discovered URLs.

        `base` is a folder the crawl must stay in, when the site we were asked about lives in
        a folder of another one (docs.pydantic.dev -> pydantic.dev/docs/).
        """
        # If the homepage redirected into locale or version folders (/en/latest/, /en-US/),
        # those folders hold the copy the site wants us to see: scope the crawl to them.
        kept = path_segments(base)
        for segment in path_segments(home.url)[len(kept) :]:
            if not (is_locale(segment) or is_version(segment)):
                break
            kept.append(segment)
        scope = "/" + "".join(segment + "/" for segment in kept)

        home_locale = kept[0] if kept and is_locale(kept[0]) else None
        # hreflang alternates are the site saying "this page in other languages lives here".
        # Other-language pages elsewhere are caught by their <html lang> (see unique_pages).
        # The base's own folder never counts: its translations (/docs/de/) are inside it.
        folders = {segments[0] for url in home.alternates if (segments := path_segments(url))}
        folders -= set(path_segments(base)[:1])
        # The brand: the domain's label, and the site name when it's one word. A longer name
        # ("MDN Web Docs") is made of ordinary words that also name real sections.
        host_label = (urlsplit(home.url).hostname or "").removeprefix("www.").split(".")[0]
        name_words = [word for word in re.split(r"\W+", site_name.lower()) if word]
        brand = {host_label} | (set(name_words) if len(name_words) == 1 else set())
        layout = cls(
            scope=scope,
            strict_scope=base != "/" or any(is_version(segment) for segment in kept),
            home_locale=home_locale,
            other_locales=frozenset(folders - {home_locale}),
            brand_words=frozenset(brand),
        )
        return replace(layout, dominant_folder=layout._dominant_folder(urls))

    def _dominant_folder(self, urls: list[str]) -> str | None:
        """The top folder holding most of the site's pages, if one does."""
        tops = Counter(
            segments[0]
            for url in urls
            if not self.is_excluded(url) and len(segments := self.section_segments(url)) >= 2
        )
        total = sum(tops.values())
        if total < MIN_URLS_FOR_DEEPER_GROUPING:
            return None
        folder, count = tops.most_common(1)[0]
        if folder in ARCHIVE_FOLDERS or count < total * DOMINANT_FOLDER_SHARE:
            return None
        return folder

    def segments(self, url: str) -> list[str]:
        """Path segments below the scope: with scope "/en/latest/", "/en/latest/api/" -> ["api"].

        URLs outside a (non-strict) scope keep all their segments: "/learn" -> ["learn"].
        """
        segments = path_segments(url)
        if self._under_scope(segments):
            return segments[self.scope.count("/") - 1 :]
        return segments

    def _under_scope(self, segments: list[str]) -> bool:
        return ("/" + "".join(segment + "/" for segment in segments)).startswith(self.scope)

    def section_segments(self, url: str) -> list[str]:
        """segments() without docs-version folders, which never make good section names."""
        return [segment for segment in self.segments(url) if not is_version(segment)]

    def is_in_dominant_folder(self, url: str) -> bool:
        segments = self.section_segments(url)
        return bool(segments) and segments[0] == self.dominant_folder

    def is_excluded(self, url: str) -> bool:
        """Copies and junk: other versions or languages, build output, login pages."""
        segments = path_segments(url)
        if self.strict_scope and not self._under_scope(segments):
            return True
        first = segments[0] if segments else ""
        if first in self.other_locales:
            return True
        # With the homepage in /en-us/, a sibling /de/ or /fr-fr/ folder is a translation.
        if self.home_locale and is_locale(first) and first != self.home_locale:
            return True
        if any(segment.rsplit(".", 1)[0] in JUNK_SEGMENTS for segment in segments):
            return True
        query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        return any(key.lower() in REDIRECT_PARAMS for key, _ in query)

    def without_version_copies(self, urls: list[str]) -> list[str]:
        """Drop versioned copies of pages that also exist unversioned (the current docs).

        /docs/2.x/intro goes when /docs/intro is present. A versioned page with no current
        twin stays; ranking then prefers the best of its versions.
        """
        current = {self.version_free_key(url) for url in urls if not self.is_versioned(url)}
        return [
            url for url in urls
            if not self.is_versioned(url) or self.version_free_key(url) not in current
        ]  # fmt: skip

    def is_versioned(self, url: str) -> bool:
        return any(is_version(segment) for segment in self.segments(url))

    def version_free_key(self, url: str) -> str:
        """The page with docs-version folders removed: /docs/3.0.1/intro and /docs/intro match."""
        kept = [segment for segment in self.segments(url) if not is_version(segment)]
        return "/".join(kept) + "?" + urlsplit(url).query

    def is_dated(self, url: str) -> bool:
        segments = self.segments(url)
        for year, month in zip(segments, segments[1:], strict=False):
            if YEAR.match(year) and MONTH.match(month):
                return True
        return any(DATE_IN_SLUG.search(segment) for segment in segments)
