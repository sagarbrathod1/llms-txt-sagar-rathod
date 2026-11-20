import asyncio
import gzip

import pytest
from fakesite import FakeSite, sitemapindex, text, urlset, xml

from app.engine import discover
from app.engine.discover import collect_sitemap_urls, fetch_robots, gunzip_capped, parse_sitemap
from app.engine.fetch import FetchError
from app.engine.models import GenerationError

ORIGIN = "https://example.com"


def robots(site):
    async def go():
        return await fetch_robots(site.fetcher(), ORIGIN)

    return asyncio.run(go())


def sitemaps(site, urls):
    async def go():
        return await collect_sitemap_urls(site.fetcher(), urls)

    return asyncio.run(go())


def test_robots_rules_apply_to_our_user_agent():
    rules = "User-agent: llms-txt-generator\nDisallow: /private\n\nUser-agent: *\nDisallow: /"
    parser = robots(FakeSite({f"{ORIGIN}/robots.txt": text(rules)}))
    assert parser.can_fetch("llms-txt-generator/0.1", f"{ORIGIN}/docs")
    assert not parser.can_fetch("llms-txt-generator/0.1", f"{ORIGIN}/private/x")


@pytest.mark.parametrize(
    ("status", "message"),
    [
        # Can't tell what's allowed, so don't crawl. (A 404 means no rules: see the pipeline.)
        (500, "Couldn't reach"),
        (429, "refusing automated requests"),
    ],
)
def test_unreadable_robots_stops_generation(status, message):
    with pytest.raises(GenerationError, match=message):
        robots(FakeSite({f"{ORIGIN}/robots.txt": (status, {}, "")}))


def test_parse_sitemap_reads_only_page_locs():
    body = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
        "<url><loc> https://example.com/a </loc>"
        "<image:image><image:loc>https://example.com/a.png</image:loc></image:image></url>"
        "</urlset>"
    )
    assert parse_sitemap(body.encode()) == (["https://example.com/a"], [])


def test_parse_sitemap_refuses_entity_expansion():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset>&a;</urlset>'
    with pytest.raises(Exception, match="(?i)entit"):
        parse_sitemap(bomb.encode())


def test_follows_sitemap_index():
    site = FakeSite(
        {
            f"{ORIGIN}/index.xml": sitemapindex(f"{ORIGIN}/pages.xml", f"{ORIGIN}/posts.xml.gz"),
            f"{ORIGIN}/pages.xml": urlset(f"{ORIGIN}/a", f"{ORIGIN}/b"),
            f"{ORIGIN}/posts.xml.gz": (
                200,
                {"content-type": "application/gzip"},
                gzip.compress(urlset(f"{ORIGIN}/post")[2].encode()),
            ),
        }
    )
    urls, warnings = sitemaps(site, [f"{ORIGIN}/index.xml"])
    assert urls == [f"{ORIGIN}/a", f"{ORIGIN}/b", f"{ORIGIN}/post"]
    assert warnings == []


def test_sitemap_recursion_is_bounded(monkeypatch):
    # Each index points to the next one; we must stop after MAX_SITEMAP_DEPTH levels.
    routes = {f"{ORIGIN}/s{n}.xml": sitemapindex(f"{ORIGIN}/s{n + 1}.xml") for n in range(10)}
    site = FakeSite(routes)
    urls, _ = sitemaps(site, [f"{ORIGIN}/s0.xml"])
    assert urls == []
    assert len(site.requests) == discover.MAX_SITEMAP_DEPTH + 1


def test_sitemap_url_and_file_caps(monkeypatch):
    monkeypatch.setattr(discover, "MAX_SITEMAP_URLS", 3)
    site = FakeSite({f"{ORIGIN}/s.xml": urlset(*(f"{ORIGIN}/p{n}" for n in range(10)))})
    urls, warnings = sitemaps(site, [f"{ORIGIN}/s.xml"])
    assert urls == [f"{ORIGIN}/p0", f"{ORIGIN}/p1", f"{ORIGIN}/p2"]
    assert "larger than we read" in warnings[0]

    monkeypatch.setattr(discover, "MAX_SITEMAPS", 2)
    children = [f"{ORIGIN}/c{n}.xml" for n in range(5)]
    routes = {f"{ORIGIN}/i.xml": sitemapindex(*children)}
    routes |= {child: urlset(f"{ORIGIN}/from-c{n}") for n, child in enumerate(children)}
    site = FakeSite(routes)
    urls, warnings = sitemaps(site, [f"{ORIGIN}/i.xml"])
    assert urls == [f"{ORIGIN}/from-c0"]  # the index plus one child
    assert len(site.requests) == 2


def test_bad_sitemaps_become_warnings():
    site = FakeSite({f"{ORIGIN}/bad.xml": xml("<urlset><url>")})
    urls, warnings = sitemaps(site, [f"{ORIGIN}/bad.xml", f"{ORIGIN}/missing.xml"])
    assert urls == []
    assert "isn't valid sitemap XML" in warnings[0]
    assert "HTTP 404" in warnings[1]


def test_gunzip_is_capped():
    bomb = gzip.compress(b"0" * 10_000)
    assert gunzip_capped(gzip.compress(b"hello"), 100) == b"hello"
    with pytest.raises(FetchError, match="too large"):
        gunzip_capped(bomb, 100)


def test_one_huge_sitemap_cannot_starve_the_others(monkeypatch):
    monkeypatch.setattr(discover, "MAX_URLS_PER_SITEMAP", 3)
    site = FakeSite(
        {
            f"{ORIGIN}/i.xml": sitemapindex(f"{ORIGIN}/products.xml", f"{ORIGIN}/pages.xml"),
            f"{ORIGIN}/products.xml": urlset(*(f"{ORIGIN}/product{n}" for n in range(100))),
            f"{ORIGIN}/pages.xml": urlset(f"{ORIGIN}/about", f"{ORIGIN}/docs"),
        }
    )
    urls, warnings = sitemaps(site, [f"{ORIGIN}/i.xml"])
    products = [f"{ORIGIN}/product{n}" for n in range(3)]
    assert urls == [*products, f"{ORIGIN}/about", f"{ORIGIN}/docs"]
    assert "larger than we read" in warnings[0]


def test_over_the_cap_version_copies_are_left_out_first(monkeypatch):
    monkeypatch.setattr(discover, "MAX_URLS_PER_SITEMAP", 3)
    listed = [f"{ORIGIN}{path}" for path in ["/docs/2.x/a", "/docs/2.x/b", "/docs/a", "/docs/b"]]
    urls, _ = sitemaps(FakeSite({f"{ORIGIN}/s.xml": urlset(*listed)}), [f"{ORIGIN}/s.xml"])
    assert urls == [f"{ORIGIN}/docs/a", f"{ORIGIN}/docs/b", f"{ORIGIN}/docs/2.x/a"]
