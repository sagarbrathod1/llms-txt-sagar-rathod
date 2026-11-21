from dataclasses import replace

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
    assert not any(is_version(s) for s in ["docs", "2026", "v", "web3", "2026-blog", "1999-2001"])


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


def test_dominant_folder_can_be_judged_again_from_more_urls():
    # Without a sitemap, the homepage's few links show no dominant folder; the crawl's do.
    layout = detect(urls=["/wiki/a", "/about", "/pricing"])
    assert layout.dominant_folder is None
    wiki = [f"{ORIGIN}/wiki/{area}/p{n}" for area in "ab" for n in range(6)]
    assert layout.with_urls([*wiki, f"{ORIGIN}/about/team"], []).dominant_folder == "wiki"


def test_docs_folders_are_grouped_deeper():
    layout = detect()
    assert layout.deep_folder(f"{ORIGIN}/docs/web/x") == "docs"
    assert layout.deep_folder(f"{ORIGIN}/guide/essentials") == "guide"
    assert layout.deep_folder(f"{ORIGIN}/blog/post") is None
    # A non-docs folder holding most of the site is grouped deeper too, but isn't docs.
    wiki = SiteLayout(dominant_folder="wiki")
    assert wiki.deep_folder(f"{ORIGIN}/wiki/a/b") == "wiki"
    assert not wiki.is_docs(f"{ORIGIN}/wiki/a/b")


def detect_with_home_links(home_links, urls):
    page = replace(home(), links=[ORIGIN + path for path in home_links])
    return SiteLayout.detect(page, "Acme", [ORIGIN + path for path in urls])


def test_the_sites_docs_need_a_docs_name_a_homepage_link_and_a_real_share_of_the_site():
    docs = [f"/docs/area/p{n}" for n in range(10)]
    products = [f"/products/p{n}" for n in range(90)]
    layout = detect_with_home_links(["/docs", "/products"], [*docs, *products])
    assert layout.docs_folders == {"docs"}
    assert layout.is_docs(f"{ORIGIN}/docs/area/p1")
    assert not layout.is_docs(f"{ORIGIN}/products/p1")
    # Not linked from the homepage: the site doesn't present it as its docs.
    assert not detect_with_home_links(["/products"], [*docs, *products]).docs_folders
    # Under 5% of the site's URLs: a /reference/ style guide on a big site.
    big = [f"/news/p{n}" for n in range(400)]
    assert not detect_with_home_links(["/docs"], [*docs, *big]).docs_folders
    # "guides" can be a store's size charts: grouped deeper, but never the site's docs.
    guides = [f"/guides/g{n}" for n in range(30)]
    store = detect_with_home_links(["/guides", "/products"], [*guides, *products])
    assert not store.docs_folders and store.deep_folder(f"{ORIGIN}/guides/g1") == "guides"


def test_in_a_docs_scope_every_page_is_docs_and_top_folders_are_its_areas():
    # docs.pydantic.dev -> pydantic.dev/docs/: /docs/ai/ and /docs/logfire/ are products.
    layout = SiteLayout.detect(home("/docs/"), "Acme", [], base="/docs/")
    assert layout.docs_scope
    assert layout.is_docs(f"{ORIGIN}/docs/ai/capabilities/mcp/")
    assert layout.deep_folder(f"{ORIGIN}/docs/ai/capabilities/mcp/") is None


def test_listing_pages():
    layout = detect()
    listings = ["/tags/python/", "/blog/category/news", "/authors/jo", "/blog/page/2",
                "/blog?page=3", "/blog?paged=4", "/2002/", "/2026/nov/", "/2026/11/20/",
                "/blog/2024/05/"]  # fmt: skip
    for path in listings:
        assert layout.is_listing(ORIGIN + path), path
    # Pagination only with a number: ?page=about is a page, WordPress's ?p=123 is a post.
    pages = ["/blog/how-to", "/2026/nov/20/a-post/", "/products/1984", "/docs/2024",
             "/index.php?page=about", "/?p=123", "/page/about", "/docs/page/intro"]  # fmt: skip
    for path in pages:
        assert not layout.is_listing(ORIGIN + path), path


def test_date_archives_are_dated_but_not_items():
    layout = detect()
    # A year archive changes as posts come out, like the posts themselves...
    assert layout.is_dated(f"{ORIGIN}/2002/")
    # ...but it's a listing, not a post: a page linking to many of them isn't a feed.
    assert not layout.is_item(f"{ORIGIN}/2002/")
    assert layout.is_item(f"{ORIGIN}/2026/11/20/a-post/")


def test_api_versions_named_by_date_count_only_in_docs():
    layout = detect()
    # Pinecone's /reference/api/2025-04/...: each dated folder is a copy of the API reference.
    assert layout.is_versioned(f"{ORIGIN}/reference/api/2025-04/chat")
    assert layout.is_versioned(f"{ORIGIN}/docs/api/2024-06-20/charges")
    assert layout.section_segments(f"{ORIGIN}/reference/api/2025-04/chat") == [
        "reference", "api", "chat"
    ]  # fmt: skip
    # Elsewhere a dated folder is an entry or an issue, not a copy of its index.
    for path in ["/changelog/2025-04-01", "/newsletter/2025-04", "/events/2025-04/agenda"]:
        assert not layout.is_versioned(ORIGIN + path), path
    assert layout.is_dated(f"{ORIGIN}/changelog/2025-04-01")  # still a dated post
    assert layout.without_version_copies(
        [f"{ORIGIN}/changelog", f"{ORIGIN}/changelog/2025-04-01"]
    ) == [f"{ORIGIN}/changelog", f"{ORIGIN}/changelog/2025-04-01"]
    # Only real months.
    assert not layout.is_versioned(f"{ORIGIN}/reference/api/2025-13/chat")


def test_a_category_page_2_is_a_listing_like_any_other():
    layout = detect()
    assert layout.is_category(f"{ORIGIN}/category/news/")
    assert not layout.is_category(f"{ORIGIN}/category/news/page/2/")
    assert layout.is_listing(f"{ORIGIN}/category/news/page/2/")
