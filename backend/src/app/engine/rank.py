"""Choose which pages go into llms.txt, and under which H2 section.

The score is a sum of a few visible signals rather than anything learned, so every placement
can be explained by pointing at the URL and the link counts.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
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
# Folders grouped one level deeper (the one holding most of the site, like MDN's /docs/, and
# docs folders) are capped in total, so their subsections can't push everything else out.
MAX_DEEP_FOLDER_PAGES = 30
# Docs are what an agent needs most: they get the first claim on this many of the MAX_PAGES
# links, before the rest of the site is ranked. Without it, a marketing site's dozens of
# shallow, well-linked pages outranked the docs (Vercel listed 6 docs pages, Linear 6).
MAX_DOCS_PAGES = MAX_DEEP_FOLDER_PAGES
# Tag, category, author, archive and page-2 pages only repeat links to other pages; a few
# show how the site is organized (simonwillison.net listed 25 year archives and 15 tags).
MAX_LISTING_PAGES = 3
# Dated posts and stories churn: each new one would change the monitored file. They rank
# below evergreen pages, and at most this many are listed, the ones the site links to most,
# so a blog still lists some posts rather than none.
MAX_DATED_PAGES = 5
# Optional pages are by definition skippable; a handful is enough to point at them.
MAX_OPTIONAL_PAGES = 10
# Job postings come and go; one or two show the site has them, the careers page lists the rest.
MAX_JOB_PAGES = 2
# Sections with fewer pages than this are merged into their parent folder's section (a lone
# /docs/web/x joins "Docs") or the general one, so the file isn't a list of one-link headings.
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
# Policy, account and job pages an agent rarely needs: they go under "## Optional".
LOW_VALUE_SEGMENTS = {
    "accessibility", "accessibility-statement", "account", "accounts", "aup", "careers", "cart",
    "checkout", "cookie-policy", "cookie-settings", "cookies", "copyright", "disclaimer", "dpa",
    "gdpr", "imprint", "impressum", "jobs", "legal", "log-in", "login", "privacy",
    "privacy-notice", "privacy-policy", "register", "sign-in", "sign-up", "signin", "signup",
    "terms", "terms-and-conditions", "terms-of-service", "terms-of-use", "tos",
    # German, French and Spanish policy pages (Spiegel's /datenschutz, /nutzungsbedingungen).
    # Matched without accents, so "mentions-légales" is "mentions-legales".
    "agb", "barrierefreiheit", "datenschutz", "datenschutzerklarung", "datenschutzerklaerung",
    "nutzungsbedingungen", "teilnahmebedingungen",
    "accessibilite", "cgu", "cgv", "conditions-generales", "conditions-generales-d-utilisation",
    "conditions-generales-de-vente", "confidentialite", "donnees-personnelles",
    "mentions-legales", "politique-de-confidentialite",
    "accesibilidad", "aviso-legal", "condiciones-de-uso", "politica-de-cookies",
    "politica-de-privacidad", "privacidad", "terminos-de-uso", "terminos-y-condiciones",
}  # fmt: skip
# Policy slugs beyond the exact names above: a policy word plus policy-ish suffixes, as in
# /privacy-security, /accessibility-policy, /cookie-preferences, /web-accessibility. Whole
# slugs only, so /legal-services, /cookie-recipes or /careers-advice stay content.
POLICY_SLUG = re.compile(
    r"^((web|website|site)-)?(acceptable-use|accessibility|cookies?|data-processing|gdpr|legal"
    r"|privacy|terms)"
    r"(-(addendum|agreement|and|center|centre|conditions|info|information|notice|of|policies"
    r"|policy|preferences|security|service|settings|statement|use))*$"
)
# File extensions some sites keep in their URLs (Patagonia's /privacy-notice.html).
PAGE_EXTENSION = re.compile(r"\.(aspx?|html?|jsp|php)$")
# Folders of job postings: /careers/<posting> is one job, /careers itself the list of them.
JOB_FOLDERS = {"career", "careers", "job", "jobs"}
# Folder levels that take turns in _round_robin; deeper folders are details of details.
ROUND_ROBIN_DEPTH = 6
# Kinds of page capped in total across sections (Ranker.kinds).
DEEP, LISTING, DATED, JOB = "deep", "listing", "dated", "job"
MAIN_CAPS = {LISTING: MAX_LISTING_PAGES, DATED: MAX_DATED_PAGES}
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
        if self.layout.is_listing(url):
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
        caps: Mapping[str, int] | None = None,
    ) -> list[str]:
        """Take up to `limit` URLs in rank order, skipping copies and capping each section.

        A copy is the same page in another docs version (version_free_key). Top-level pages
        aren't capped: they're usually the site's main pages. `caps` limits kinds of pages
        across all sections (see kinds). `already` continues an earlier pick: its URLs count
        toward every cap and are returned first.
        """
        caps = caps or {}
        taken: list[str] = []
        keys: set[str] = set()
        counts: Counter[str | None] = Counter()
        kind_counts: Counter[str] = Counter()
        previous = set(already)
        for url in [*already, *(url for url in ranked if url not in previous)]:
            if len(taken) == limit:
                break
            key = self.layout.version_free_key(url)
            folder = self.section_folder(url)
            kinds = self.kinds(url)
            if url not in previous and (
                key in keys
                or (folder is not None and counts[folder] >= per_section)
                or any(kind_counts[kind] >= caps[kind] for kind in kinds if kind in caps)
            ):
                continue
            keys.add(key)
            counts[folder] += 1
            kind_counts.update(kinds)
            taken.append(url)
        return taken

    def pick_spread(
        self,
        ranked: list[str],
        limit: int,
        per_section: int,
        sections: set[str | None] | None = None,
        caps: Mapping[str, int] | None = None,
        already: Sequence[str] = (),
    ) -> list[str]:
        """Pick round-robin across sections, then fill any room left by rank.

        Round-robin: folders take turns at every level of the URL tree, so the pick covers
        a site's areas before going deep into any one of them (see _round_robin). With a
        per-section cap alone, a few big sections used the whole budget and smaller ones
        (Bootstrap's Layout, Forms, Utilities; Cursor's CLI docs) were never reached.
        Only sections in `sections` (default: all) take turns. Old docs versions, listing
        pages and dated posts never do; they can only fill what's left. `already` continues
        an earlier pick, as in pick().
        """
        eligible = [
            url for url in ranked
            if (sections is None or self.section_folder(url) in sections)
            and not self.layout.is_versioned(url)
            and not self.kinds(url) & {LISTING, DATED}
        ]  # fmt: skip
        round_robin = _round_robin(eligible, self.layout.section_segments)
        taken = self.pick(round_robin, limit, per_section, already, caps)
        return self.pick(ranked, limit, per_section, already=taken, caps=caps)

    def pick_to_fetch(self, ranked: list[str], limit: int) -> list[str]:
        """Pick sitemap pages to fetch, the way build_sections will pick from them: docs
        first (up to MAX_DOCS_PAGES), then the rest, each round-robin across the sections the
        fetched pages link into, then by rank.

        Top-level pages and folders the fetched pages link into (their landing page or any
        page in them) take turns; folders only a sitemap knows about don't: gov.uk's sitemap
        has 41 folders, and a turn for each fetched 1988 air-accident reports and tribunal
        decisions. With nothing linking anywhere yet (an app's empty homepage), every
        section takes turns.
        """
        linked = {
            segments[0]
            for url, count in self.inlinks.items()
            if count and (segments := self.layout.section_segments(url))
        }
        sections = {self.section_folder(url) for url in ranked}
        sections = {f for f in sections if f is None or f.split("/")[0] in linked}
        sections = sections if linked else None
        docs = [url for url in ranked if self.layout.is_docs(url)]
        taken = self.pick_spread(docs, min(limit, MAX_DOCS_PAGES), limit, sections)
        return self.pick_spread(ranked, limit, limit, sections, already=taken)

    def kinds(self, url: str) -> set[str]:
        """The kinds of page `url` is that are capped in total (see the MAX_*_PAGES caps)."""
        kinds = set()
        if self.layout.deep_folder(url):
            kinds.add(DEEP)
        if self.layout.is_listing(url):
            kinds.add(LISTING)
        if self.layout.is_dated(url):
            kinds.add(DATED)
        segments = self.layout.segments(url)
        if len(segments) >= 2 and segments[0] in JOB_FOLDERS:
            kinds.add(JOB)
        return kinds

    def section_folder(self, url: str) -> str | None:
        """The folder path that names this page's section, or None for the general section.

        "/blog/post" -> "blog". In a deep folder (docs, or the folder holding most of the
        site), "/docs/web/x" -> "docs/web", and "/docs/intro" stays in "docs", so a flat
        folder is still one section for the per-section cap (and for a link trap while
        following links). Top-level pages (/pricing) and dated paths (/2026/05/x) have none,
        and neither does a folder called "optional", which would clash with the spec's
        Optional section.
        """
        segments = self.layout.section_segments(url)
        if len(segments) < 2:
            return None
        if self.layout.deep_folder(url):
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
        segments = [self._slug(segment) for segment in self.layout.segments(url)]
        if not segments or self.layout.is_listing(url):  # /tags/privacy lists posts on privacy
            return False
        if _is_policy_name(segments[0]):
            return True
        # The last folder only near the root: /help/terms-conditions is a policy, a deep
        # /docs/web/privacy is content. /docs/privacy-features never matches a policy name.
        return len(segments) <= 2 and _is_policy_name(segments[-1])

    def _slug(self, segment: str) -> str:
        """A path segment as a policy name: "privacy-notice.html" -> "privacy-notice",
        "mentions-légales" -> "mentions-legales", and the site's own name dropped,
        "datenschutz-spiegel" -> "datenschutz" (unless nothing else is left)."""
        segment = PAGE_EXTENSION.sub("", segment)
        segment = unicodedata.normalize("NFKD", segment)
        segment = "".join(char for char in segment if not unicodedata.combining(char))
        words = [word for word in segment.split("-") if word not in self.layout.brand_words]
        return "-".join(words) or segment

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
        main = self.pick_main(regular)
        optional = [url for url in ranked if self.is_low_value(url)]
        optional = self.pick(
            optional, MAX_OPTIONAL_PAGES, MAX_OPTIONAL_PAGES, caps={JOB: MAX_JOB_PAGES}
        )

        groups: dict[str | None, list[str]] = {}
        for url in main:  # rank order, so each group's first URL is its best
            groups.setdefault(self.section_folder(url), []).append(url)
        # A docs area too small to stand alone (a lone /docs/web/x) joins its parent, "docs".
        self._merge_small_groups(groups, subsections=True)
        # A page whose own path is a section's folder is that section's landing page
        # (/docs for "docs", /blog for "blog"): it leads the section. A docs landing leads
        # a "Docs" section even alone when the docs' areas are sections of their own.
        for folder, urls in list(groups.items()):
            for url in list(urls):
                own = "/".join(self.layout.section_segments(url))
                if own not in groups and _has_subsections(groups, own):
                    groups[own] = []
                if own != folder and own in groups:
                    urls.remove(url)
                    groups[own].insert(0, url)
        groups = {folder: urls for folder, urls in groups.items() if urls}
        # Any other section too small to stand alone joins the general one.
        self._merge_small_groups(groups, subsections=False)

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

    @staticmethod
    def _merge_small_groups(groups: dict[str | None, list[str]], subsections: bool) -> None:
        """Move groups with fewer than MIN_SECTION_SIZE pages into their parent folder's
        group ("docs/web" -> "docs"), or the general one (None) for top folders."""
        for folder in [f for f in groups if f and ("/" in f) == subsections]:
            if len(groups[folder]) < MIN_SECTION_SIZE and not _has_subsections(groups, folder):
                parent = folder.rpartition("/")[0] or None
                groups.setdefault(parent, []).extend(groups.pop(folder))

    def pick_main(self, regular: list[str]) -> list[str]:
        """The pages for the main sections, from `regular` in rank order.

        1. Docs first, up to MAX_DOCS_PAGES, round-robin across their areas (pick_spread), so
           one big area can't take them all.
        2. Then the rest of the site by rank, with deep folders capped in total, so their
           subsections can't crowd out everything else.
        3. Then any room left by rank, without that cap (a site that's almost all docs).
        Listing and dated pages are capped throughout.
        """
        docs = [url for url in regular if self.layout.is_docs(url)]
        main = self.pick_spread(docs, MAX_DOCS_PAGES, MAX_PAGES_PER_SECTION, caps=MAIN_CAPS)
        caps = MAIN_CAPS | {DEEP: MAX_DEEP_FOLDER_PAGES}
        main = self.pick(regular, MAX_PAGES, MAX_PAGES_PER_SECTION, already=main, caps=caps)
        return self.pick(regular, MAX_PAGES, MAX_PAGES_PER_SECTION, already=main, caps=MAIN_CAPS)

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


def _has_subsections(groups: dict[str | None, list[str]], folder: str) -> bool:
    return any(other and other.startswith(folder + "/") for other in groups)


def _round_robin(ranked: list[str], segments: Callable[[str], list[str]]) -> list[str]:
    """`ranked` reordered so that folders take turns, at every level of the URL tree.

    In each folder, the queues are its own pages (its landing page and the pages directly in
    it) and each subfolder, ordered the same way recursively. The queues give one page per
    turn: /docs/guides/auth, /docs/reference/javascript, then /docs/guides/database, ... A
    subfolder with a single page joins its parent's own pages, as build_sections will place
    it. Queues go in the order of their best page, so within a round it's still rank order.
    Below ROUND_ROBIN_DEPTH folders, pages simply queue in rank order: that bounds the
    recursion, whatever URLs a site links to.
    """
    position = {url: index for index, url in enumerate(ranked)}
    paths = {url: tuple(segments(url)[:ROUND_ROBIN_DEPTH]) for url in ranked}
    folders = {path[:depth] for path in paths.values() for depth in range(len(path))}
    # A page lives in its parent folder, or in its own path if other pages are under it:
    # /docs/cli is the landing page of /docs/cli/.
    home = {url: path if path in folders else path[:-1] for url, path in paths.items()}
    size = Counter(home[url][:depth] for url in ranked for depth in range(len(home[url]) + 1))

    def order(folder: tuple[str, ...], urls: list[str]) -> list[str]:
        queues: dict[tuple[str, ...], list[str]] = {}  # filled in rank order
        for url in urls:
            child = home[url][: len(folder) + 1]
            alone = len(child) > len(folder) and size[child] >= MIN_SECTION_SIZE
            queues.setdefault(child if alone else folder, []).append(url)
        turns = [order(queue, members) if queue != folder else members
                 for queue, members in queues.items()]  # fmt: skip
        turns.sort(key=lambda queue: position[queue[0]])
        return [queue[turn] for turn in range(max(map(len, turns), default=0))
                for queue in turns if turn < len(queue)]  # fmt: skip

    return order((), ranked)


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
