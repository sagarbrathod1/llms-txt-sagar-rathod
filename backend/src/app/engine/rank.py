"""Choose which pages go into llms.txt, and under which H2 section.

The score is a sum of a few visible signals rather than anything learned, so every placement
can be explained by pointing at the URL and the link counts.
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from urllib.parse import urlsplit

from app.engine.extract import Page
from app.engine.layout import YEAR, SiteLayout
from app.engine.models import PageInfo

# llms.txt is read into a model's context window, and the spec asks for concise files.
# 50 links at ~150 characters each is ~7.5 KB (~2k tokens): enough to cover the main areas of
# most sites while staying a small fraction of any context window.
MAX_PAGES = 50
# One area (a blog, a guides library) can have hundreds of pages that all score alike. Capping
# each section at under a third of MAX_PAGES leaves room for the rest of the site.
MAX_PAGES_PER_SECTION = 15
# The folder holding most of the site (MDN's /docs/) is grouped one level deeper; this caps
# its total, so its subsections can't push everything else out of the file.
MAX_DOMINANT_FOLDER_PAGES = 30
# Optional pages are by definition skippable; a handful is enough to point at them.
MAX_OPTIONAL_PAGES = 10
# Sections with fewer pages than this are merged into the general section, so the file
# doesn't become a long list of one-link headings.
MIN_SECTION_SIZE = 2
GENERAL_SECTION = "Pages"
# When the general section is long, it's split by the site's own navigation: pages the
# homepage links to, and the rest.
HOME_LINKED_SECTION = "Main pages"
OTHER_SECTION = "More pages"
OPTIONAL_SECTION = "Optional"  # the name the spec gives to skippable links

# Inlinks reward pages the site itself links to a lot (navigation, footers). Capped so a page
# linked from every page doesn't drown out every other signal.
MAX_INLINKS_COUNTED = 10

# First path segments that usually lead to the content an LLM wants: docs, product, pricing.
BOOSTED_SEGMENTS = {
    "about", "api", "docs", "documentation", "examples", "features", "getting-started",
    "guide", "guides", "integrations", "learn", "platform", "pricing", "product", "products",
    "quickstart", "reference", "solutions", "tutorial", "tutorials",
}  # fmt: skip
# Listing pages that only repeat links to other pages.
LISTING_SEGMENTS = {"archive", "archives", "author", "authors", "category", "categories",
                    "page", "tag", "tags"}  # fmt: skip
# Policy, account and job pages an agent rarely needs: they go under "## Optional".
LOW_VALUE_SEGMENTS = {
    "accessibility", "accessibility-statement", "account", "accounts", "careers", "cart",
    "checkout", "cookie-policy", "cookie-settings", "cookies", "copyright", "disclaimer",
    "gdpr", "imprint", "impressum", "jobs", "legal", "log-in", "login", "privacy",
    "privacy-notice", "privacy-policy", "register", "sign-in", "sign-up", "signin", "signup",
    "terms", "terms-and-conditions", "terms-of-service", "terms-of-use", "tos",
}  # fmt: skip
# Policy slugs beyond the exact names above: a policy word plus policy-ish suffixes, as in
# /privacy-security, /accessibility-policy, /cookie-preferences. Whole slugs only, so
# /legal-services, /cookie-recipes or /careers-advice stay content.
POLICY_SLUG = re.compile(
    r"^(accessibility|cookies?|gdpr|legal|privacy|terms)"
    r"(-(and|center|centre|conditions|info|information|notice|of|policies|policy|preferences"
    r"|security|service|settings|statement|use))*$"
)
# Words in section names that are abbreviations despite containing vowels.
ACRONYMS = {"ai", "api", "cli", "eu", "faq", "ios", "seo", "til", "ui", "uk", "url", "us"}


class Ranker:
    """Everything ranking needs to know about one site, with the decisions that use it."""

    def __init__(
        self,
        layout: SiteLayout,
        inlinks: dict[str, int],
        order: dict[str, int],
        home_links: set[str],
    ):
        self.layout = layout
        self.inlinks = inlinks
        # Discovery order: homepage links first (in page order), then sitemap order. Used to
        # break ties, so equal scores follow the site's own priorities, deterministically.
        self.order = order
        self.home_links = home_links

    def score(self, url: str) -> float:
        """Higher is more important. Used both to pick which pages to fetch and which to keep."""
        segments = self.layout.segments(url)
        total = -len(segments)  # shallow pages are usually overviews; deep ones are details
        total += min(self.inlinks.get(url, 0), MAX_INLINKS_COUNTED)
        if segments and segments[0] in BOOSTED_SEGMENTS:
            total += 2
        if any(segment in LISTING_SEGMENTS for segment in segments):
            total -= 5
        if self.layout.is_versioned(url):
            total -= 4  # an old or upcoming docs version; the current one should win
        if self.layout.is_dated(url):
            # News and posts churn daily, and the monitored file would churn with them. More
            # than the inlink cap, so today's top stories (linked from every page) can't
            # outrank evergreen pages; among dated pages the order is unchanged.
            total -= MAX_INLINKS_COUNTED + 2
        if urlsplit(url).query:
            total -= 2  # query strings are usually filters or sort orders of another page
        return total

    def rank(self, urls: list[str]) -> list[str]:
        """URLs best-first; ties follow discovery order, then the URL, so it's deterministic."""
        never_seen = len(self.order)
        return sorted(
            urls, key=lambda url: (-self.score(url), self.order.get(url, never_seen), url)
        )

    def pick(
        self,
        ranked: list[str],
        limit: int,
        per_section: int,
        already: Sequence[str] = (),
        cap_dominant: bool = False,
    ) -> list[str]:
        """Take up to `limit` URLs in rank order, skipping copies and capping each section.

        A copy is the same page in another docs version (version_free_key). Top-level pages
        aren't capped: they're usually the site's main pages. With cap_dominant, the folder
        holding most of the site is also capped in total. `already` continues an earlier
        pick: its URLs count toward every cap and are returned first.
        """
        taken: list[str] = []
        keys: set[str] = set()
        counts: Counter[str | None] = Counter()
        in_dominant = 0
        previous = set(already)
        for url in [*already, *(url for url in ranked if url not in previous)]:
            if len(taken) == limit:
                break
            key = self.layout.version_free_key(url)
            folder = self.section_folder(url)
            dominant = self.layout.is_in_dominant_folder(url)
            if url not in previous and (
                key in keys
                or (folder is not None and counts[folder] == per_section)
                or (cap_dominant and dominant and in_dominant == MAX_DOMINANT_FOLDER_PAGES)
            ):
                continue
            keys.add(key)
            counts[folder] += 1
            in_dominant += dominant
            taken.append(url)
        return taken

    def pick_spread(self, ranked: list[str], limit: int) -> list[str]:
        """Pick for fetching: an even share of `limit` per section first, then fill by rank.

        With a fixed per-section cap, a few big sections used the whole budget and smaller
        ones (Bootstrap's Layout, Forms, Utilities) were never fetched.
        """
        sections = {self.section_folder(url) for url in ranked}
        share = math.ceil(limit / max(len(sections), 1))
        taken = self.pick(ranked, limit, share)
        return self.pick(ranked, limit, limit, already=taken)

    def section_folder(self, url: str) -> str | None:
        """The folder path that names this page's section, or None for the general section.

        "/docs/intro" -> "docs". When "docs" holds most of the site, "/docs/web/x" ->
        "docs/web" (and "/docs/intro" stays in "docs"). Top-level pages (/pricing) and dated
        paths (/2026/05/x) have none, and neither does a folder called "optional", which
        would clash with the spec's Optional section.
        """
        segments = self.layout.section_segments(url)
        if len(segments) < 2:
            return None
        if segments[0] == self.layout.dominant_folder:
            # /docs/web/x -> "docs/web"; pages right under it (/docs/cli) form "docs" itself.
            segments = segments[:2] if len(segments) > 2 else segments[:1]
        else:
            segments = segments[:1]
        if YEAR.match(segments[-1]) or segments[-1] == "optional":
            return None
        return "/".join(segments)

    def section_title(self, folder: str) -> str:
        """ "getting-started" -> "Getting started", "psf" -> "PSF", "about-npr" -> "About".

        Abbreviations are upper-cased: known ones, and 2-4 letter words without vowels, which
        can't be read as words. The site's own name is dropped as redundant.
        """
        words = [word for word in re.split(r"[-_\s]+", folder.rsplit("/", 1)[-1]) if word]
        words = [word for word in words if word not in self.layout.brand_words] or words
        words = [
            word.upper()
            if word in ACRONYMS or (2 <= len(word) <= 4 and not set(word) & set("aeiouy"))
            else word
            for word in words
        ]
        name = " ".join(words)
        return name[:1].upper() + name[1:]

    def is_low_value(self, url: str) -> bool:
        """Policy and account pages. Matched on the first or last folder only, and the last
        only near the root: /legal/terms and /about/privacy are policies, but a deep
        /docs/web/privacy page is content about privacy."""
        segments = self.layout.segments(url)
        if not segments:
            return False
        if _is_policy_name(segments[0]):
            return True
        # Deeper down, only an exact name: /docs/privacy-features is content, not a policy.
        return len(segments) <= 2 and segments[-1] in LOW_VALUE_SEGMENTS

    def build_sections(self, pages: list[PageInfo]) -> list[tuple[str, list[PageInfo]]]:
        """Select the top pages and group them into ordered (section name, pages) pairs.

        The input pages' `section` is ignored; the returned copies have it filled in.
        Sections are ordered by their best page, so the most important section comes first,
        and Optional is always last. Pages within a section keep rank order, except that a
        section's landing page (/docs next to /docs/intro) leads it.
        """
        by_url = {page.url: page for page in pages}
        ranked = self.rank(self.layout.without_version_copies(list(by_url)))
        regular = [url for url in ranked if not self.is_low_value(url)]
        # The dominant folder's cap makes room for the rest of the site; if the rest can't
        # use it (a site that's almost all docs), the fill pass gives it back.
        main = self.pick(regular, MAX_PAGES, MAX_PAGES_PER_SECTION, cap_dominant=True)
        main = self.pick(regular, MAX_PAGES, MAX_PAGES_PER_SECTION, already=main)
        optional = [url for url in ranked if self.is_low_value(url)]
        optional = self.pick(optional, MAX_OPTIONAL_PAGES, MAX_OPTIONAL_PAGES)

        groups: dict[str | None, list[str]] = {}
        for url in main:  # rank order, so each group's first URL is its best
            groups.setdefault(self.section_folder(url), []).append(url)
        # A page whose own path is a section's folder is that section's landing page
        # (/docs for "docs", /docs/glossary for "docs/glossary"): it leads the section.
        for folder, urls in list(groups.items()):
            for url in list(urls):
                own = "/".join(self.layout.section_segments(url))
                if own != folder and own in groups:
                    urls.remove(url)
                    groups[own].insert(0, url)
        groups = {folder: urls for folder, urls in groups.items() if urls}
        for folder in [f for f, urls in groups.items() if f and len(urls) < MIN_SECTION_SIZE]:
            groups.setdefault(None, []).extend(groups.pop(folder))

        # Two folders can get the same title ("about" and "about-acme"); merge them.
        by_title: dict[str, list[str]] = {}
        sections = [(self.section_title(f), urls) for f, urls in groups.items() if f is not None]
        for title, urls in sections + self._general_sections(groups.get(None, [])):
            merged = by_title.get(title)
            by_title[title] = self.rank(merged + urls) if merged else urls
        position = {url: index for index, url in enumerate(ranked)}
        named = sorted(
            by_title.items(), key=lambda item: (min(position[url] for url in item[1]), item[0])
        )
        if optional:
            named.append((OPTIONAL_SECTION, optional))
        return [(name, [replace(by_url[u], section=name) for u in urls]) for name, urls in named]

    def _general_sections(self, urls: list[str]) -> list[tuple[str, list[str]]]:
        """Top-level and leftover pages; split by homepage navigation when there are many."""
        urls = self.rank(urls)  # merged groups were appended out of rank order
        if len(urls) <= MAX_PAGES_PER_SECTION:
            return [(GENERAL_SECTION, urls)] if urls else []
        linked = [url for url in urls if url in self.home_links]
        other = [url for url in urls if url not in self.home_links]
        if len(linked) < MIN_SECTION_SIZE or len(other) < MIN_SECTION_SIZE:
            return [(GENERAL_SECTION, urls)]
        return [(HOME_LINKED_SECTION, linked), (OTHER_SECTION, other)]


def _is_policy_name(segment: str) -> bool:
    return segment in LOW_VALUE_SEGMENTS or bool(POLICY_SLUG.match(segment))


def in_other_language(page: Page, home: Page) -> bool:
    """An edition in another language (/cymraeg, /persian): its <html lang> differs from the
    homepage's. Only when both declare one; many pages don't."""
    return bool(page.lang and home.lang and page.lang != home.lang)


def listing_url(page: Page, is_excluded: Callable[[str], bool]) -> str:
    """The URL a page is listed and counted under: its canonical, unless the crawl itself
    would exclude that URL. Django's /en/6.1/ pages name /en/dev/ (another docs version) as
    canonical, which would put one version's links among another's."""
    if page.canonical and not is_excluded(page.canonical):
        return page.canonical
    return page.url


def unique_pages(
    pages: list[Page], home: Page, is_excluded: Callable[[str], bool] = lambda url: False
) -> dict[str, Page]:
    """Listable pages keyed by their canonical URL: no noindex pages, no duplicates, no homepage,
    nothing in another language.

    Pages whose own URL is their canonical go first, then by URL, so which duplicate wins
    doesn't depend on fetch order.
    """
    home_urls = {home.url, home.canonical}
    chosen: dict[str, Page] = {}

    def is_duplicate_first(page: Page) -> tuple[bool, str]:
        return (page.url != listing_url(page, is_excluded), page.url)

    # /about and /about/ are usually one page served twice; same title and description
    # confirm it. (Not always the same page, so the URL alone isn't enough.)
    seen_content: set[tuple[str, str | None, str | None]] = set()
    for page in sorted(pages, key=is_duplicate_first):
        key = listing_url(page, is_excluded)
        content = (key.rstrip("/"), page.title, page.description)
        if page.noindex or key in chosen or key in home_urls or page.url in home_urls:
            continue
        if in_other_language(page, home):
            continue
        if content in seen_content:
            continue
        seen_content.add(content)
        chosen[key] = page
    return chosen


def inlink_counts(
    pages: list[Page], is_excluded: Callable[[str], bool] = lambda url: False
) -> Counter[str]:
    """For each URL, how many distinct fetched pages link to it (self-links don't count)."""
    canonical = {page.url: listing_url(page, is_excluded) for page in pages}
    counts: Counter[str] = Counter()
    for page in pages:
        own = canonical[page.url]
        counts.update({canonical.get(link, link) for link in page.links} - {own})
    return counts
