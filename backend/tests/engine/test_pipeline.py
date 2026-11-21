import asyncio
import random
import re
import threading
import time

import httpx2
import pytest
from fakesite import FakeSite, page, redirect, text, urlset
from reference_parser import assert_round_trips

from app.engine import crawl, extract, fetch, pipeline
from app.engine.models import GenerationError

ORIGIN = "https://example.com"
NAV = ["/docs/start", "/docs/api", "/pricing", "/privacy"]


def home(**kwargs):
    defaults = {
        "title": "Home | Acme",
        "description": "Acme makes widgets.",
        "links": NAV,
        "head": '<meta property="og:site_name" content="Acme">',
    }
    return page(**(defaults | kwargs))


def site_routes():
    """A small site whose sitemap is too short, so pages are found by following links."""
    return {
        f"{ORIGIN}/": home(),
        f"{ORIGIN}/docs/start": page("Getting started | Acme", "Install.", links=["/docs/api"]),
        f"{ORIGIN}/docs/api": page("API reference - Acme", "Endpoints.", links=["/docs/start"]),
        f"{ORIGIN}/pricing": page("Pricing | Acme", "Acme makes widgets."),  # copies home's
        f"{ORIGIN}/privacy": page("Privacy policy | Acme"),
    }


def run(site, origin=ORIGIN, on_progress=None):
    """Run the pipeline; every output must also pass the validator and the reference parser."""

    async def go():
        return await pipeline.run(origin, on_progress, site.fetcher())

    result = asyncio.run(go())
    blockquote = re.search(r"^> (.*)$", result.llms_txt, re.MULTILINE)
    assert_round_trips(result.llms_txt, blockquote and blockquote.group(1), result.pages)
    return result


def test_pages_are_parsed_one_at_a_time(monkeypatch):
    """Regression: parses ran in parallel threads. The parser is pure Python, so they only took
    turns on the GIL, and together they starved the event loop that also serves the API."""
    real_extract_page = extract.extract_page
    lock = threading.Lock()
    running = most_at_once = 0

    def tracked(*args):
        nonlocal running, most_at_once
        with lock:
            running += 1
            most_at_once = max(most_at_once, running)
        try:
            time.sleep(0.02)  # long enough for parallel parses to overlap
            return real_extract_page(*args)
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(extract, "extract_page", tracked)
    run(FakeSite(site_routes()))
    assert most_at_once == 1


def test_golden_site():
    result = run(FakeSite(site_routes()))
    assert (
        result.llms_txt
        == """\
# Acme

> Acme makes widgets.

Key pages of https://example.com/, grouped by section. Links under Optional can be skipped \
when a shorter context is needed.

## Docs

- [Getting started](https://example.com/docs/start): Install.
- [API reference](https://example.com/docs/api): Endpoints.

## Pages

- [Pricing](https://example.com/pricing)

## Optional

- [Privacy policy](https://example.com/privacy)
"""
    )
    # Equal scores follow the homepage's own link order (/docs/start before /docs/api).
    assert [(p.url, p.section) for p in result.pages] == [
        (f"{ORIGIN}/docs/start", "Docs"),
        (f"{ORIGIN}/docs/api", "Docs"),
        (f"{ORIGIN}/pricing", "Pages"),
        (f"{ORIGIN}/privacy", "Optional"),
    ]
    assert result.pages_attempted == 5  # homepage + 4 pages
    assert result.pages_failed == 0


def test_uses_sitemap_urls():
    pages = [f"{ORIGIN}/docs/p{n}" for n in range(12)]
    routes = {f"{ORIGIN}/": home(links=[]), f"{ORIGIN}/sitemap.xml": urlset(*pages)}
    routes |= {url: page(f"Page {url[-2:]}") for url in pages}
    result = run(FakeSite(routes))
    assert len(result.pages) == 12
    assert not any("following links" in w for w in result.warnings)


def test_bfs_fallback_follows_links_to_depth_limit():
    # A chain /l1 -> /l2 -> ... ; only BFS_MAX_DEPTH levels below the homepage are fetched.
    routes = {f"{ORIGIN}/": home(links=["/l1"])}
    routes |= {f"{ORIGIN}/l{n}": page(f"Level {n}", links=[f"/l{n + 1}"]) for n in range(1, 10)}
    site = FakeSite(routes)
    result = run(site)
    assert [p.title for p in result.pages] == ["Level 1", "Level 2", "Level 3"]
    assert f"{ORIGIN}/l4" not in site.requests
    assert any("following links" in w for w in result.warnings)


def test_obeys_robots():
    rules = "User-agent: *\nDisallow: /docs/"
    site = FakeSite(site_routes() | {f"{ORIGIN}/robots.txt": text(rules)})
    result = run(site)
    assert not any("/docs/" in url for url in site.requests)
    assert "Docs" not in result.llms_txt
    assert any("robots.txt disallows 2" in w for w in result.warnings)


def test_robots_disallowing_everything_is_an_error():
    site = FakeSite(site_routes() | {f"{ORIGIN}/robots.txt": text("User-agent: *\nDisallow: /")})
    with pytest.raises(GenerationError, match="robots.txt doesn't allow"):
        run(site)
    assert site.requests == [f"{ORIGIN}/robots.txt"]


def test_skips_noindex_and_dedupes_by_canonical():
    routes = site_routes() | {
        f"{ORIGIN}/": home(links=[*NAV, "/hidden", "/docs/start?ref=nav", "/docs/start-copy"]),
        f"{ORIGIN}/hidden": page("Hidden", head='<meta name="robots" content="noindex">'),
        f"{ORIGIN}/docs/start?ref=nav": page(
            "Start (nav)", head='<link rel="canonical" href="/docs/start">'
        ),
        f"{ORIGIN}/docs/start-copy": page("Copy", head='<link rel="canonical" href="/docs/start">'),
    }
    result = run(FakeSite(routes))
    urls = [p.url for p in result.pages]
    assert f"{ORIGIN}/hidden" not in urls
    assert urls.count(f"{ORIGIN}/docs/start") == 1
    assert "Getting started" in result.llms_txt  # the canonical page itself wins
    assert any("noindex" in w for w in result.warnings)


def test_counts_transient_failures_but_not_missing_pages():
    routes = site_routes() | {f"{ORIGIN}/docs/api": (503, {}, "")}
    del routes[f"{ORIGIN}/pricing"]  # 404
    result = run(FakeSite(routes))
    assert result.pages_failed == 1
    assert result.pages_attempted == 5  # the 404 and the failure were attempts too
    assert [p.url for p in result.pages] == [f"{ORIGIN}/docs/start", f"{ORIGIN}/privacy"]
    assert any("couldn't be fetched" in w and "HTTP 503" in w for w in result.warnings)
    assert any("skipped" in w and "HTTP 404" in w for w in result.warnings)


def test_page_timeouts_become_failures_not_exceptions(monkeypatch):
    # The worker treats a builtin TimeoutError as its own job limit, so one must never escape.
    monkeypatch.setattr(fetch, "TOTAL_TIMEOUT_SECONDS", 0.05)
    site = FakeSite(site_routes())
    real_handler = site.handler

    async def slow_handler(request):
        if request.url.path == "/docs/api":
            await asyncio.sleep(1)
        return real_handler(request)

    site.handler = slow_handler
    result = run(site)
    assert result.pages_failed == 1
    assert any("timed out" in w for w in result.warnings)


def test_follows_homepage_redirect_to_www():
    www = "https://www.example.com"
    site = FakeSite(
        {
            f"{ORIGIN}/": redirect(f"{www}/"),
            f"{www}/": home(links=["/docs/start"]),
            f"{www}/docs/start": page("Getting started | Acme"),
        }
    )
    result = run(site)
    assert [p.url for p in result.pages] == [f"{www}/docs/start"]
    assert f"{www}/robots.txt" in site.requests  # the new origin's robots.txt is obeyed
    assert any("redirects to https://www.example.com" in w for w in result.warnings)


def test_ssrf_blocked_site_is_an_error():
    site = FakeSite(site_routes(), dns={"example.com": "127.0.0.1"})
    with pytest.raises(GenerationError, match="private or internal"):
        run(site)


def test_homepage_redirect_to_private_ip_is_an_error():
    # robots.txt is on a public address; only the homepage's redirect leads inside.
    site = FakeSite(
        {f"{ORIGIN}/": redirect("http://internal.example.com/")},
        dns={"internal.example.com": "10.0.0.5"},
    )
    with pytest.raises(GenerationError, match="private or internal"):
        run(site)
    assert "http://internal.example.com/" not in site.requests


def test_unreachable_site_is_an_error():
    site = FakeSite({})
    site.handler = lambda request: (_ for _ in ()).throw(httpx2.ConnectError("refused"))
    with pytest.raises(GenerationError, match="Couldn't reach"):
        run(site)


@pytest.mark.parametrize("status", [403, 406])
def test_bot_blocked_homepage_is_an_error(status):
    with pytest.raises(GenerationError, match=f"HTTP {status}; the site may block automated"):
        run(FakeSite({f"{ORIGIN}/": (status, {}, "")}))


def test_homepage_only_site_warns():
    result = run(FakeSite({f"{ORIGIN}/": home(links=[])}))
    assert result.llms_txt == (
        "# Acme\n\n> Acme makes widgets.\n\n"
        "The homepage is https://example.com/; no other pages could be listed.\n"
    )
    assert any("JavaScript" in w for w in result.warnings)


def test_a_page_that_breaks_the_parser_is_skipped_not_fatal(monkeypatch):
    real_extract = extract.extract_page

    def flaky_extract(body, url, *args):
        if url.endswith("/pricing"):
            raise RuntimeError("parser bug")
        return real_extract(body, url, *args)

    monkeypatch.setattr(extract, "extract_page", flaky_extract)
    result = run(FakeSite(site_routes()))
    assert "Pricing" not in result.llms_txt
    assert "Getting started" in result.llms_txt
    assert any("couldn't be parsed" in w for w in result.warnings)


def test_a_429_storm_is_an_error_not_a_degraded_file():
    routes = site_routes()
    for url in [f"{ORIGIN}/docs/start", f"{ORIGIN}/docs/api", f"{ORIGIN}/pricing"]:
        routes[url] = (429, {}, "")
    many = [f"/p{n}" for n in range(8)]
    routes[f"{ORIGIN}/"] = home(links=[*NAV, *many])
    routes |= {f"{ORIGIN}{path}": (429, {}, "") for path in many}
    with pytest.raises(GenerationError, match="rate-limiting our crawler"):
        run(FakeSite(routes))


def test_mostly_failing_site_is_an_error():
    routes = site_routes()
    for url in [f"{ORIGIN}/docs/start", f"{ORIGIN}/docs/api", f"{ORIGIN}/pricing"]:
        routes[url] = (503, {}, "")
    with pytest.raises(GenerationError, match="3 of the 4 pages"):
        run(FakeSite(routes))


def test_crawl_delay_is_honored_and_reported():
    site = FakeSite(site_routes() | {f"{ORIGIN}/robots.txt": text("User-agent: *\nCrawl-delay: 9")})
    result = run(site)
    assert "Getting started" in result.llms_txt
    # The spacing actually used is stated: a long delay is capped (at 0.05 s in these tests).
    assert any(
        "asks for 9 s between requests, so pages were fetched one at a time, 0.05 s apart "
        "(our maximum)" in w
        for w in result.warnings
    )


def test_fetch_budget_fits_the_time_left():
    class Spaced:
        spacing = 5.0

        def seconds_left(self):
            return 100.0

    assert crawl.fetch_budget(Spaced()) == 20
    Spaced.spacing = 0.0
    assert crawl.fetch_budget(Spaced()) == crawl.MAX_PAGES_FETCHED


def test_deadline_renders_what_was_fetched(monkeypatch):
    monkeypatch.setattr(pipeline, "RUN_DEADLINE_SECONDS", 0.25)
    routes = {f"{ORIGIN}/": home(links=["/l1"])}
    routes |= {f"{ORIGIN}/l{n}": page(f"Level {n}", links=[f"/l{n + 1}"]) for n in range(1, 4)}
    site = FakeSite(routes)
    inner = site.handler

    async def slow(request):
        if request.url.path.startswith("/l"):
            await asyncio.sleep(0.15)
        return inner(request)

    site.handler = slow
    seen = []
    result = run(site, on_progress=seen.append)
    assert "Level 1" in result.llms_txt
    assert "Level 3" not in result.llms_txt
    assert any("time limit" in w for w in result.warnings)
    # Pages the deadline cut off count as attempted and failed, so a truncated recheck
    # can't replace a complete version.
    assert result.pages_failed >= 1
    assert result.pages_attempted == result.pages_failed + 1 + len(result.pages)
    assert seen[-1].pages_crawled == seen[-1].pages_discovered  # the bar still fills


def test_translations_are_not_fetched_or_listed():
    alternates = '<link rel="alternate" hreflang="de" href="/de/">'
    alternates += '<link rel="alternate" hreflang="x-default" href="/">'
    docs = [f"{ORIGIN}/docs/p{n}" for n in range(6)]
    german = [f"{ORIGIN}/de/docs/p{n}" for n in range(6)]
    routes = {
        f"{ORIGIN}/": home(links=["/docs/p0"], head=alternates),
        f"{ORIGIN}/sitemap.xml": urlset(*docs, *german),
    }
    routes |= {url: page(f"Page {url[-1]}") for url in docs + german}
    site = FakeSite(routes)
    result = run(site)
    assert len(result.pages) == 6
    assert not any("/de/" in url for url in site.requests)


def test_versioned_docs_are_scoped_to_where_the_homepage_redirects():
    latest = [f"{ORIGIN}/en/latest/api/p{n}" for n in range(6)]
    stable = [f"{ORIGIN}/en/stable/api/p{n}" for n in range(6)]
    routes = {
        f"{ORIGIN}/": redirect("/en/latest/"),
        f"{ORIGIN}/en/latest/": home(links=["api/p0", "/en/stable/"]),
        f"{ORIGIN}/sitemap.xml": urlset(*latest, *stable),
    }
    routes |= {url: page(f"P{url[-1]} | Acme") for url in latest + stable}
    site = FakeSite(routes)
    result = run(site)
    assert [p.url for p in result.pages] == latest
    assert {p.section for p in result.pages} == {"API"}  # named below the /en/latest/ scope
    assert not any("/en/stable/" in url for url in site.requests)


def test_boilerplate_shared_within_a_section_is_stripped():
    """Regression (BBC): each section has its own suffix ("| BBC Travel", "| BBC News"), too
    rare site-wide to count as boilerplate, so every link kept it."""
    travel = [f"{ORIGIN}/travel/t{n}" for n in range(4)]
    news = [f"{ORIGIN}/news/n{n}" for n in range(4)]
    other = [f"{ORIGIN}/p{n}" for n in range(10)]
    routes = {
        f"{ORIGIN}/": home(links=[]),
        f"{ORIGIN}/sitemap.xml": urlset(*travel, *news, *other),
    }
    routes |= {url: page(f"Trip {url[-1]} | Acme Travel") for url in travel}
    routes |= {url: page(f"Story {url[-1]} | Latest | Acme News") for url in news}
    routes |= {url: page(f"Page {url[-1]}") for url in other}
    result = run(FakeSite(routes))
    titles = {p.url: p.title for p in result.pages}
    assert titles[travel[0]] == "Trip 0"
    assert titles[news[0]] == "Story 0"  # both layers of the news suffix


def test_a_canonical_into_another_docs_version_is_ignored():
    """Regression (Django): /en/6.1/ pages name /en/dev/ as canonical. Listing those URLs put
    another version's links in the file, under a section named after the locale ("En")."""
    latest = [f"{ORIGIN}/en/latest/api/p{n}" for n in range(6)]
    routes = {
        f"{ORIGIN}/": redirect("/en/latest/"),
        f"{ORIGIN}/en/latest/": home(links=["api/p0"]),
        f"{ORIGIN}/sitemap.xml": urlset(*latest),
    }
    routes |= {
        url: page(
            f"P{url[-1]} | Acme",
            head=f'<link rel="canonical" href="{url.replace("latest", "dev")}">',
        )
        for url in latest
    }
    result = run(FakeSite(routes))
    assert [p.url for p in result.pages] == latest
    assert {p.section for p in result.pages} == {"API"}


def test_a_link_trap_cannot_use_up_the_budget(monkeypatch):
    monkeypatch.setattr(crawl, "MAX_PAGES_FETCHED", 25)
    # Every calendar page links to the next month, forever; the real pages sit beside it.
    routes = {f"{ORIGIN}/": home(links=["/calendar/m1", "/docs/a", "/docs/b"])}
    routes |= {
        f"{ORIGIN}/calendar/m{n}": page(f"Month {n}", links=[f"/calendar/m{n + 1}"])
        for n in range(1, 500)
    }
    routes |= {f"{ORIGIN}/docs/{p}": page(f"Doc {p}") for p in "ab"}
    site = FakeSite(routes)
    result = run(site)
    assert len([u for u in site.requests if "/calendar/" in u]) <= 3  # one per BFS level
    assert len(site.requests) <= 25 + 2  # budget + robots.txt + sitemap.xml
    assert "Doc a" in result.llms_txt and "Doc b" in result.llms_txt


def test_sitemap_mode_respects_the_fetch_budget(monkeypatch):
    monkeypatch.setattr(crawl, "MAX_PAGES_FETCHED", 20)
    urls = [f"{ORIGIN}/section{n % 7}/p{n}" for n in range(300)]
    routes = {f"{ORIGIN}/": home(links=[]), f"{ORIGIN}/sitemap.xml": urlset(*urls)}
    routes |= {url: page(f"Page {url}") for url in urls}
    site = FakeSite(routes)
    seen = []
    result = run(site, on_progress=seen.append)
    page_requests = [u for u in site.requests if u in urls]
    assert len(page_requests) == 19  # the budget includes the homepage
    assert result.pages_attempted == 20
    # The progress bar counts planned fetches, not the 300 URLs the sitemap listed.
    assert max(p.pages_discovered for p in seen) == 20


def test_output_is_deterministic():
    # Pages finish in a random order each run; the file must come out the same every time.
    def site_with_jitter(seed):
        site = FakeSite(site_routes())
        inner, rng = site.handler, random.Random(seed)

        async def jittery(request):
            await asyncio.sleep(rng.random() / 50)
            return inner(request)

        site.handler = jittery
        return site

    outputs = {run(site_with_jitter(seed)).llms_txt for seed in range(5)}
    assert len(outputs) == 1


def test_homepage_without_meta_description_uses_its_first_paragraph():
    intro = "<main><p>Acme makes widgets for people who need them to last forever.</p></main>"
    routes = site_routes() | {f"{ORIGIN}/": home(description=None, body=intro)}
    result = run(FakeSite(routes))
    assert "> Acme makes widgets for people who need them to last forever.\n" in result.llms_txt


def test_trailing_slash_twins_are_listed_once():
    routes = site_routes() | {
        f"{ORIGIN}/": home(links=[*NAV, "/pricing/"]),
        f"{ORIGIN}/pricing/": site_routes()[f"{ORIGIN}/pricing"],
    }
    urls = [p.url for p in run(FakeSite(routes)).pages]
    assert urls.count(f"{ORIGIN}/pricing") + urls.count(f"{ORIGIN}/pricing/") == 1


def test_site_wide_meta_description_gives_way_to_the_page_paragraph():
    boilerplate = "The official home of Acme."
    routes = site_routes() | {
        f"{ORIGIN}/docs/start": page(
            "Getting started | Acme", boilerplate,
            body="<main><p>Install Acme with one command and configure it in a minute.</p></main>",
        ),
        f"{ORIGIN}/docs/api": page("API reference - Acme", boilerplate),
    }  # fmt: skip
    text = run(FakeSite(routes)).llms_txt
    assert "(https://example.com/docs/start): Install Acme with one command" in text
    assert "(https://example.com/docs/api)\n" in text  # nothing page-specific to say
    assert boilerplate not in text


def test_relative_sitemap_line_in_robots_is_resolved():
    pages = [f"{ORIGIN}/essays/e{n}" for n in range(12)]
    routes = {
        f"{ORIGIN}/robots.txt": text("User-agent: *\nAllow: /\nSitemap: /maps/site.xml"),
        f"{ORIGIN}/maps/site.xml": urlset(*pages),
        f"{ORIGIN}/": home(links=[]),
    }
    routes |= {url: page(f"Essay {url[-2:]}") for url in pages}
    result = run(FakeSite(routes))
    assert len(result.pages) == 12
    assert not any("sitemap" in w.lower() for w in result.warnings)


@pytest.mark.parametrize(
    ("robots", "warned"),
    [
        (None, False),  # /sitemap.xml was only a guess: its 404 is no news
        ("User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml", True),  # promised
    ],
)
def test_missing_sitemap_is_a_warning_only_if_robots_listed_it(robots, warned):
    routes = site_routes() | ({f"{ORIGIN}/robots.txt": text(robots)} if robots else {})
    result = run(FakeSite(routes))
    assert any("Couldn't read the sitemap" in w for w in result.warnings) is warned


def test_shift_jis_page_with_header_charset():
    title = "青空文庫"
    body = f"<html><head><title>{title} | Acme</title></head><body></body></html>"
    shift_jis = (200, {"content-type": "text/html; charset=Shift_JIS"}, body.encode("shift_jis"))
    routes = site_routes() | {f"{ORIGIN}/docs/start": shift_jis}
    assert f"[{title}](https://example.com/docs/start)" in run(FakeSite(routes)).llms_txt


def test_oversized_page_is_truncated_but_still_listed(monkeypatch):
    # Pages are read with crawl's copy of the limit; 500 bytes still covers this <head>.
    huge = page("Getting started | Acme", "Install.", body="<script>" + "x" * 10_000 + "</script>")
    routes = site_routes() | {f"{ORIGIN}/docs/start": huge}
    monkeypatch.setattr(crawl, "MAX_PAGE_BYTES", 500)
    assert (
        "[Getting started](https://example.com/docs/start): Install."
        in run(FakeSite(routes)).llms_txt
    )


def test_undecodable_bytes_do_not_print_bs4_warnings(caplog):
    broken = (200, {"content-type": "text/html; charset=utf-8"}, b"<title>Bad \xff\xfe</title>")
    routes = site_routes() | {f"{ORIGIN}/docs/start": broken}
    with caplog.at_level("WARNING"):
        run(FakeSite(routes))
    assert not any("could not be decoded" in record.message for record in caplog.records)


def test_a_site_wide_banner_paragraph_is_skipped_for_the_next_one():
    banner = "<p>Try the new Acme cloud today, the fastest way to deploy your widgets.</p>"
    routes = site_routes() | {
        f"{ORIGIN}/docs/start": page(
            "Getting started | Acme",
            body=(
                f"<main>{banner}<p>Install Acme with one command and configure it fast.</p></main>"
            ),
        ),
        f"{ORIGIN}/docs/api": page("API reference - Acme", body=f"<main>{banner}</main>"),
    }
    text = run(FakeSite(routes)).llms_txt
    assert "/docs/start): Install Acme with one command and configure it fast." in text
    assert "Try the new Acme cloud" not in text


def test_dated_pages_cannot_outrank_evergreen_ones_through_inlinks():
    story = "/2026/11/19/big-story"
    routes = site_routes() | {
        f"{ORIGIN}/": home(links=[story, *NAV]),
        f"{ORIGIN}/docs/start": page("Getting started | Acme", links=["/docs/api", story]),
        f"{ORIGIN}/docs/api": page("API reference - Acme", links=["/docs/start", story]),
        f"{ORIGIN}/pricing": page("Pricing | Acme", links=[story]),
        f"{ORIGIN}{story}": page("Big story | Acme"),
    }
    urls = [p.url for p in run(FakeSite(routes)).pages]
    assert urls.index(f"{ORIGIN}{story}") > urls.index(f"{ORIGIN}/pricing")


def test_sitemap_mode_also_fetches_homepage_navigation():
    # A news-style sitemap lists only articles; the homepage links to the evergreen sections.
    stories = [f"{ORIGIN}/2026/11/{n:02}/story" for n in range(1, 13)]
    routes = {
        f"{ORIGIN}/": home(links=["/sections/news/", "/sections/science/"]),
        f"{ORIGIN}/sitemap.xml": urlset(*stories),
        f"{ORIGIN}/sections/news/": page("News | Acme"),
        f"{ORIGIN}/sections/science/": page("Science | Acme"),
    }
    routes |= {url: page(f"Story {url}") for url in stories}
    urls = [p.url for p in run(FakeSite(routes)).pages]
    assert urls[:2] == [f"{ORIGIN}/sections/news/", f"{ORIGIN}/sections/science/"]


def test_other_language_editions_are_left_out_by_html_lang():
    def lang_page(title, lang):
        return f'<html lang="{lang}"><head><title>{title}</title></head><body></body></html>'

    english_home = home(links=[*NAV, "/cymraeg", "/persian"]).replace("<html>", '<html lang="en">')
    routes = site_routes() | {
        f"{ORIGIN}/": english_home,
        f"{ORIGIN}/cymraeg": lang_page("Croeso", "cy"),
        f"{ORIGIN}/persian": lang_page("خانه", "fa"),
    }
    result = run(FakeSite(routes))
    assert "Croeso" not in result.llms_txt and "/persian" not in result.llms_txt
    assert "Getting started" in result.llms_txt  # pages without a lang attribute stay
    assert any("another language" in w for w in result.warnings)


def test_a_site_refusing_most_pages_is_an_error():
    routes = site_routes()
    for url in [f"{ORIGIN}/docs/start", f"{ORIGIN}/docs/api", f"{ORIGIN}/pricing"]:
        routes[url] = (403, {}, "")
    with pytest.raises(GenerationError, match="blocking our crawler"):
        run(FakeSite(routes))


def test_a_few_refused_pages_count_as_failures():
    routes = site_routes() | {f"{ORIGIN}/pricing": (403, {}, "")}
    result = run(FakeSite(routes))
    assert result.pages_failed == 1  # so a blocked recheck can't replace a good version


def test_malformed_sitemap_line_in_robots_is_skipped():
    rules = text("User-agent: *\nAllow: /\nSitemap: http://[bad")
    result = run(FakeSite(site_routes() | {f"{ORIGIN}/robots.txt": rules}))
    assert "Getting started" in result.llms_txt
    assert any("malformed sitemap URL" in w for w in result.warnings)


def test_progress_only_moves_forward_and_ends_full():
    # Following links discovers pages as it goes; the bar must still never jump back.
    routes = {f"{ORIGIN}/": home(links=["/a", "/b"])}
    routes |= {f"{ORIGIN}/{p}": page(p, links=[f"/{p}{q}" for q in "xyz"]) for p in "ab"}
    routes |= {f"{ORIGIN}/{p}{q}": page(p + q) for p in "ab" for q in "xyz"}
    seen = []
    run(FakeSite(routes), on_progress=seen.append)
    ratios = [p.pages_crawled / p.pages_discovered for p in seen]
    assert ratios == sorted(ratios)
    assert ratios[-1] == 1


def test_description_repeating_the_title_is_dropped():
    routes = site_routes() | {f"{ORIGIN}/pricing": page("Pricing | Acme", "Pricing")}
    assert "- [Pricing](https://example.com/pricing)\n" in run(FakeSite(routes)).llms_txt
    # Regression (svelte.dev): the title plus a suffix ("Routing • SvelteKit documentation").
    echo = page("Pricing • Acme", "Pricing • Acme documentation")
    routes = site_routes() | {f"{ORIGIN}/pricing": echo}
    assert "- [Pricing](https://example.com/pricing)\n" in run(FakeSite(routes)).llms_txt
    # ...but a longer remainder is content (gov.uk).
    more = "Pricing - compare the plans, see what each includes and pay yearly to save"
    routes = site_routes() | {f"{ORIGIN}/pricing": page("Pricing | Acme", more)}
    assert f"- [Pricing](https://example.com/pricing): {more}\n" in run(FakeSite(routes)).llms_txt


def test_summary_equal_to_the_site_name_is_dropped():
    routes = site_routes() | {f"{ORIGIN}/": home(description="Acme")}
    assert "\n> " not in run(FakeSite(routes)).llms_txt


def test_detail_line_names_the_site_origin_not_the_redirected_homepage():
    routes = site_routes() | {f"{ORIGIN}/": redirect("/en/home"), f"{ORIGIN}/en/home": home()}
    text = run(FakeSite(routes)).llms_txt
    assert "Key pages of https://example.com/, grouped" in text


def bot_check(title="Client Challenge"):
    """What bot-protection services serve instead of the page, with HTTP 200."""
    return page(title, body="<p>Please enable JavaScript to continue.</p>")


def test_a_site_answering_with_bot_checks_is_an_error():
    """Regression (Le Monde): 49 of 50 links were titled "Client Challenge", with no warning."""
    routes = site_routes()
    for url in [f"{ORIGIN}/docs/start", f"{ORIGIN}/docs/api", f"{ORIGIN}/pricing"]:
        routes[url] = bot_check()
    with pytest.raises(GenerationError, match=r'bot check \("Client Challenge"\) instead of 3'):
        run(FakeSite(routes))


def test_a_bot_check_homepage_is_an_error():
    routes = site_routes() | {f"{ORIGIN}/": bot_check("Just a moment...")}
    with pytest.raises(GenerationError, match="bot check .* instead of its homepage"):
        run(FakeSite(routes))


def test_a_few_bot_check_pages_are_left_out_and_counted_as_failures():
    routes = site_routes() | {f"{ORIGIN}/pricing": bot_check("Attention Required! | Cloudflare")}
    result = run(FakeSite(routes))
    assert "Attention Required" not in result.llms_txt and "/pricing" not in result.llms_txt
    assert result.pages_failed == 1  # a blocked recheck mustn't replace a good version
    # Reported as what it was: bot checks aren't retried, unlike timeouts.
    assert any("1 page(s) answered with a bot check" in w for w in result.warnings)
    assert not any("couldn't be fetched" in w for w in result.warnings)


def test_a_real_page_whose_title_looks_like_a_bot_check_is_kept():
    """Regression: ddos-guard.net's homepage, "DDoS-Guard | Reliable DDoS Protection", was
    reported as a bot check. A challenge page has no links into the site; a real page does."""
    routes = site_routes() | {
        f"{ORIGIN}/": home(title="DDoS-Guard | Reliable DDoS Protection", links=NAV),
        f"{ORIGIN}/docs/start": page("Access Denied - Troubleshooting | Acme", links=["/"]),
        f"{ORIGIN}/docs/api": page("Just a Moment | A blog post", links=["/docs/start"]),
    }
    result = run(FakeSite(routes))
    assert "[Access Denied - Troubleshooting]" in result.llms_txt
    assert "[Just a Moment | A blog post]" in result.llms_txt
    assert result.pages_failed == 0


@pytest.mark.parametrize(
    ("home_title", "shell_title"),
    [
        ("Acme • The app for widgets", "Acme • The app for widgets"),  # homepage is the shell
        ("Acme – Project tracking", "Acme App"),  # a prerendered homepage, a different shell
        ("Acme – Project tracking", "Page not found"),  # a stale sitemap of HTTP 200 404s
        ("Acme – Project tracking", None),  # an untitled shell
    ],
)
def test_identical_empty_pages_warn_and_are_titled_from_their_urls(home_title, shell_title):
    """Regression (hoppscotch.io): every route serves the same empty shell, and all 43 links
    were titled "Hoppscotch • Open source API development ecosystem". Pages that can't be
    told apart are no evidence of a bot check, so this warns rather than fails."""
    routes = {f"{ORIGIN}/": page(home_title, "Acme makes widgets.", links=NAV)}
    routes |= {f"{ORIGIN}{path}": page(shell_title) for path in NAV}
    result = run(FakeSite(routes))
    assert sorted(p.title for p in result.pages) == ["Api", "Pricing", "Privacy", "Start"]
    assert any("the same nearly empty page" in w and "may not exist" in w for w in result.warnings)


def test_feed_pages_are_described_by_their_meta_description_or_not_at_all():
    """Regression (The Verge, simonwillison.net): a section listing dated or numbered stories
    was described by its latest story, so the file changed daily."""
    stories = "".join(f'<a href="/news/{100000 + n}/story">Story {n}</a>' for n in range(30))
    latest = "<p>The latest story of the day, which will be a different one tomorrow.</p>"
    routes = site_routes() | {
        f"{ORIGIN}/": home(links=[*NAV, "/energy"]),
        f"{ORIGIN}/energy": page("Energy | Acme", body=f"<main>{latest}{stories}</main>"),
    }
    text = run(FakeSite(routes)).llms_txt
    assert "- [Energy](https://example.com/energy)\n" in text
    assert "latest story" not in text


def test_an_article_with_a_long_sidebar_of_stories_is_not_a_feed():
    """Regression (krebsonsecurity.com, kubernetes.io's blog, Google Help): articles whose
    sidebars list dozens of dated posts lost their own descriptions."""
    sidebar = "".join(f'<a href="/2026/11/{n:02}/post">Post {n}</a>' for n in range(1, 31))
    own = "<p>This article explains how the breach happened and what to do about it.</p>"
    article = "/2026/11/19/the-breach"
    routes = site_routes() | {
        f"{ORIGIN}/": home(links=[*NAV, article]),
        f"{ORIGIN}{article}": page("The breach | Acme", body=f"<main>{own}{sidebar}</main>"),
    }
    text = run(FakeSite(routes)).llms_txt
    assert "the-breach): This article explains how the breach happened" in text


def test_hub_pages_are_described_by_their_meta_description_or_not_at_all():
    """Regression (The Verge, BBC, Spiegel): a section front was described by the teaser of
    today's top story, so the file, and its monitored version, changed daily."""
    headlines = "".join(
        f'<li><a href="/2026/11/{n}/story">Headline {n} today</a></li>' for n in range(20)
    )
    intro = "<p>The top story of the day, which will be a different one tomorrow morning.</p>"
    card = '<div><h2><a href="/2026/11/9/story">Top story</a></h2>' + intro + "</div>"
    routes = site_routes() | {
        f"{ORIGIN}/": home(links=[*NAV, "/news", "/sport"]),
        f"{ORIGIN}/news": page("News | Acme", body=f"<main>{intro}<ul>{headlines}</ul></main>"),
        f"{ORIGIN}/sport": page("Sport | Acme", "All the sport.", body=f"<main>{card}</main>"),
    }
    text = run(FakeSite(routes)).llms_txt
    assert "- [News](https://example.com/news)\n" in text  # a hub: no paragraph
    assert "- [Sport](https://example.com/sport): All the sport.\n" in text
    assert "top story" not in text


@pytest.mark.parametrize("landing", ["/docs/", "/docs", "/docs/intro"])
def test_a_redirect_into_a_folder_of_another_site_scopes_the_crawl(landing):
    """Regression (docs.pydantic.dev -> pydantic.dev/docs/, docs.linear.app -> linear.app/docs):
    the whole other site was crawled, giving none of the docs the user asked about."""
    other = "https://other.com"
    docs = [f"{other}/docs/guide/p{n}" for n in range(6)]
    routes = {
        f"{ORIGIN}/": redirect(f"{other}{landing}"),
        f"{other}{landing}": home(links=["/docs/guide/p0", "/pricing", "/blog/post"]),
        f"{other}/sitemap.xml": urlset(*docs, f"{other}/pricing", f"{other}/careers"),
        f"{other}/pricing": page("Pricing | Other"),
        f"{other}/blog/post": page("Post | Other"),
    }
    routes |= {url: page(f"Page {url[-1]} | Acme") for url in docs}
    site = FakeSite(routes)
    result = run(site)
    assert [p.url for p in result.pages if p.url.startswith(f"{other}/docs/guide/")] == docs
    assert not any(url.endswith(("/pricing", "/careers", "/post")) for url in site.requests)
    assert "Key pages of https://other.com/docs/, grouped" in result.llms_txt
    assert any(
        "redirects to https://other.com/docs, so that part of the site" in w
        for w in result.warnings
    )


def test_a_redirect_to_a_lone_landing_page_of_another_site_crawls_that_site():
    # /welcome links to nothing under /welcome/: it's a landing page, not a section.
    other = "https://other.com"
    routes = {
        f"{ORIGIN}/": redirect(f"{other}/welcome"),
        f"{other}/welcome": home(links=["/docs/start", "/pricing"]),
        f"{other}/docs/start": page("Getting started | Acme"),
        f"{other}/pricing": page("Pricing | Acme"),
    }
    result = run(FakeSite(routes))
    assert {p.url for p in result.pages} == {f"{other}/docs/start", f"{other}/pricing"}
    assert "Key pages of https://other.com/, grouped" in result.llms_txt


def test_a_scoped_crawl_follows_links_when_the_sitemap_has_little_in_scope():
    """Regression: 12 marketing URLs in the root sitemap made a crawl scoped to /docs/ pick
    sitemap mode, which then had almost nothing to fetch, and listed one page."""
    other = "https://other.com"
    marketing = [f"{other}/m{n}" for n in range(12)]
    routes = {
        f"{ORIGIN}/": redirect(f"{other}/docs/"),
        f"{other}/docs/": home(links=["/docs/a"]),
        f"{other}/sitemap.xml": urlset(*marketing),
        f"{other}/docs/a": page("A | Acme", links=["/docs/b"]),
        f"{other}/docs/b": page("B | Acme", links=["/docs/c"]),
        f"{other}/docs/c": page("C | Acme"),
    }
    result = run(FakeSite(routes))
    assert [p.title for p in result.pages] == ["A", "B", "C"]


def test_a_redirect_within_the_site_does_not_scope_the_crawl():
    # Patagonia's homepage redirects to /home/; the rest of the site is still the site.
    routes = site_routes() | {f"{ORIGIN}/": redirect("/home/"), f"{ORIGIN}/home/": home()}
    assert "Getting started" in run(FakeSite(routes)).llms_txt


def test_a_text_file_listed_as_a_sitemap_is_skipped_quietly():
    """Regression (Pinecone): robots.txt lists llms.txt as a sitemap, which gave a confusing
    "isn't valid sitemap XML" warning."""
    rules = "User-agent: *\nAllow: /\nSitemap: https://example.com/llms.txt"
    site = FakeSite(site_routes() | {f"{ORIGIN}/robots.txt": text(rules)})
    result = run(site)
    assert not any("sitemap XML" in w for w in result.warnings)
    assert f"{ORIGIN}/llms.txt" not in site.requests


def test_a_site_refusing_with_406_and_bot_checks_is_an_error():
    """Regression (Le Monde, later): most pages answered HTTP 406, the rest a bot check, so
    neither alone was a majority and a one-link file was published."""
    routes = site_routes()
    many = [f"/p{n}" for n in range(6)]
    routes[f"{ORIGIN}/"] = home(links=[*NAV, *many])
    routes |= {f"{ORIGIN}{path}": (406, {}, "") for path in many}
    routes[f"{ORIGIN}/docs/start"] = bot_check()
    routes[f"{ORIGIN}/docs/api"] = bot_check()
    with pytest.raises(
        GenerationError, match=r"blocking our crawler: 8 of the 10 .*406.*bot check"
    ):
        run(FakeSite(routes))
