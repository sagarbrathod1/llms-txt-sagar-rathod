from app.engine.extract import Page
from app.engine.layout import SiteLayout, is_locale, is_version

ORIGIN = "https://example.com"


def home(path="/", alternates=()):
    return Page(
        url=ORIGIN + path, canonical=None, title=None, description=None, paragraphs=[],
        is_hub=False, site_name=None, alt_site_names=[], lang=None,
        noindex=False, links=[], alternates=[ORIGIN + a for a in alternates],
    )  # fmt: skip


def detect(path="/", alternates=(), urls=(), name="Acme"):
    return SiteLayout.detect(home(path, alternates), name, [ORIGIN + u for u in urls])


def test_locale_and_version_segments():
    assert all(is_locale(s) for s in ["en", "en-US", "pt_br", "zh-hans", "es-419"])
    assert not any(is_locale(s) for s in ["ai", "go", "tv", "docs", "eng"])
    assert all(is_version(s) for s in ["2.x", "3.0.1", "v2", "2.0.0-alpha.30", "latest", "stable"])
    assert not any(is_version(s) for s in ["docs", "2026", "v", "web3"])


def test_version_scope_is_strict():
    layout = detect("/en/latest/")
    assert layout.scope == "/en/latest/" and layout.strict_scope
    assert layout.segments(f"{ORIGIN}/en/latest/api/x") == ["api", "x"]
    assert layout.is_excluded(f"{ORIGIN}/en/stable/api/x")  # the other version tree
    assert layout.is_excluded(f"{ORIGIN}/blog/x")  # outside a version scope
    assert not layout.is_excluded(f"{ORIGIN}/en/latest/api/x")
    assert detect("/welcome").scope == "/"  # an ordinary page: no scoping


def test_locale_scope_keeps_non_localized_content():
    # nodejs.org redirects to /en but keeps /learn and /api outside it.
    layout = detect("/en")
    assert layout.scope == "/en/" and not layout.strict_scope
    assert not layout.is_excluded(f"{ORIGIN}/learn/getting-started")
    assert not layout.is_excluded(f"{ORIGIN}/en/download")
    assert layout.segments(f"{ORIGIN}/en/download/x") == ["download", "x"]
    assert layout.segments(f"{ORIGIN}/learn/x") == ["learn", "x"]
    # A sibling locale folder is a translation.
    assert layout.is_excluded(f"{ORIGIN}/fr/download")
    assert layout.is_excluded(f"{ORIGIN}/pt-br/learn")
    assert detect("/en-US/").scope == "/en-us/"


def test_other_locales_from_hreflang():
    layout = detect("/", alternates=["/de/", "/fr-fr/"])
    assert layout.other_locales == {"de", "fr-fr"}
    assert layout.is_excluded(f"{ORIGIN}/de/docs")
    assert not layout.is_excluded(f"{ORIGIN}/docs")


def test_language_code_folders_alone_are_not_excluded():
    # /hr/, /it/, /is/ are often departments; other-language pages are caught by <html lang>.
    layout = detect(urls=["/hr/jobs", "/it/help", "/is/team", "/de/a"])
    assert not any(layout.is_excluded(f"{ORIGIN}{p}") for p in ["/hr/jobs", "/it/help", "/de/a"])


def test_home_locale_is_never_excluded():
    layout = detect("/en/", alternates=["/en/", "/de/"])
    assert layout.other_locales == {"de"}
    assert not layout.is_excluded(f"{ORIGIN}/en/docs")


def test_junk_and_login_urls_are_excluded():
    layout = detect()
    for path in ["/_modules/requests/api", "/genindex.html", "/search?q=x",
                 "/accounts/login/?next=/jobs/", "/login?redirect=/cart"]:  # fmt: skip
        assert layout.is_excluded(ORIGIN + path), path


def test_version_free_key():
    layout = detect()
    key = layout.version_free_key
    assert key(f"{ORIGIN}/docs/3.0.1/intro") == key(f"{ORIGIN}/docs/intro")
    assert key(f"{ORIGIN}/docs/next/intro") == key(f"{ORIGIN}/docs/intro")
    assert key(f"{ORIGIN}/docs/intro") != key(f"{ORIGIN}/docs/setup")


def test_dated_urls():
    layout = detect()
    assert layout.is_dated(f"{ORIGIN}/2026/11/19/story")
    assert layout.is_dated(f"{ORIGIN}/news/2026-11-19-story")
    assert layout.is_dated(f"{ORIGIN}/world/2026/nov/19/story")
    assert not layout.is_dated(f"{ORIGIN}/docs/2026-roadmap")
    assert not layout.is_dated(f"{ORIGIN}/products/1984")


def test_brand_words_come_from_domain_and_one_word_site_names():
    assert detect(name="NPR").brand_words == {"npr", "example"}
    assert detect(name="MDN Web Docs").brand_words == {"example"}  # ordinary words


def test_finds_a_dominant_folder():
    docs = [f"/docs/web/p{n}" for n in range(8)] + [f"/docs/glossary/g{n}" for n in range(6)]
    assert detect(urls=[*docs, "/blog/a", "/blog/b"]).dominant_folder == "docs"
    balanced = [f"/docs/p{n}" for n in range(6)] + [f"/blog/p{n}" for n in range(6)]
    assert detect(urls=balanced).dominant_folder is None
    assert detect(urls=["/docs/a", "/docs/b"]).dominant_folder is None  # too few to judge


def test_version_copies_go_when_a_current_twin_exists():
    layout = detect()
    urls = [f"{ORIGIN}/docs/2.x/intro", f"{ORIGIN}/docs/intro", f"{ORIGIN}/docs/2.x/old-only"]
    assert layout.without_version_copies(urls) == [
        f"{ORIGIN}/docs/intro",
        f"{ORIGIN}/docs/2.x/old-only",  # no current twin: kept
    ]


def test_archive_folders_are_never_grouped_deeper():
    posts = [f"/blog/2024/p{n}" for n in range(14)]
    assert detect(urls=[*posts, "/learn/a", "/api/b"]).dominant_folder is None
    # Nor are listing folders: martinfowler.com's homepage links to 72 tag pages.
    tags = [f"/tags/t{n}.html" for n in range(14)]
    assert detect(urls=[*tags, "/bliki/a", "/books/b"]).dominant_folder is None


def test_listing_pages():
    layout = detect()
    listings = ["/tags/python/", "/blog/category/news", "/authors/jo", "/blog/page/2",
                "/blog?page=3", "/2002/", "/2026/nov/", "/2026/11/20/",
                "/blog/2024/05/"]  # fmt: skip
    for path in listings:
        assert layout.is_listing(ORIGIN + path), path
    for path in ["/blog/how-to", "/2026/nov/20/a-post/", "/products/1984", "/docs/2024"]:
        assert not layout.is_listing(ORIGIN + path), path


def test_date_archives_are_dated_but_not_items():
    layout = detect()
    # A year archive changes as posts come out, like the posts themselves...
    assert layout.is_dated(f"{ORIGIN}/2002/")
    # ...but it's a listing, not a post: a page linking to many of them isn't a feed.
    assert not layout.is_item(f"{ORIGIN}/2002/")
    assert layout.is_item(f"{ORIGIN}/2026/11/20/a-post/")
