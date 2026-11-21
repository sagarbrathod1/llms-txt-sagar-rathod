from app.engine import rank as rank_module
from app.engine.layout import SiteLayout
from app.engine.models import PageInfo
from app.engine.rank import Ranker

ORIGIN = "https://example.com"


def info(path):
    return PageInfo(url=ORIGIN + path, title=path, description=None, section="")


def ranker(inlinks=None, order=None, home_links=(), layout=None):
    layout = layout or SiteLayout(brand_words=frozenset({"acme"}))
    return Ranker(layout, inlinks or {}, order or {}, {ORIGIN + p for p in home_links})


def sections_of(pages, **kwargs):
    sections = ranker(**kwargs).build_sections([info(p) for p in pages])
    return [(name, [p.url.removeprefix(ORIGIN) for p in ps]) for name, ps in sections]


def test_shallow_pages_outrank_deep_ones():
    r = ranker()
    assert r.score(f"{ORIGIN}/blog") > r.score(f"{ORIGIN}/blog/2024/05/post")


def test_inlinks_count_but_are_capped():
    url = f"{ORIGIN}/a/b/c"
    assert ranker({url: 5}).score(url) > ranker().score(url)
    capped = rank_module.MAX_INLINKS_COUNTED
    assert ranker({url: 1000}).score(url) == ranker({url: capped}).score(url)


def test_boosts_and_penalties():
    r = ranker()
    assert r.score(f"{ORIGIN}/docs/x") > r.score(f"{ORIGIN}/misc/x")  # docs boosted
    assert r.score(f"{ORIGIN}/tag/python") < r.score(f"{ORIGIN}/misc/x")  # listing page
    assert r.score(f"{ORIGIN}/shop?sort=price") < r.score(f"{ORIGIN}/shop")  # query string
    assert r.score(f"{ORIGIN}/docs/2.x/intro") < r.score(f"{ORIGIN}/docs/intro")  # old version
    assert r.score(f"{ORIGIN}/news/2026/11/story") < r.score(f"{ORIGIN}/news/a/story")  # dated


def test_ties_follow_discovery_order_then_url():
    urls = [f"{ORIGIN}/b", f"{ORIGIN}/a", f"{ORIGIN}/c"]
    assert ranker().rank(urls) == [f"{ORIGIN}/a", f"{ORIGIN}/b", f"{ORIGIN}/c"]
    order = {f"{ORIGIN}/c": 0, f"{ORIGIN}/a": 1}  # e.g. the homepage nav lists /c first
    assert ranker(order=order).rank(urls) == [f"{ORIGIN}/c", f"{ORIGIN}/a", f"{ORIGIN}/b"]


def test_section_titles():
    r = ranker()
    assert r.section_title("getting-started") == "Getting started"
    assert r.section_title("api_reference") == "API reference"
    assert r.section_title("psf") == "PSF"  # no vowels: an abbreviation
    assert r.section_title("about-acme") == "About"  # the site's own name is redundant
    assert r.section_title("acme") == "Acme"  # ...unless nothing else is left
    assert r.section_title("my-account") == "My account"  # "my" has a vowel (y)
    assert r.section_title("us-news") == "US news"
    assert r.section_title("til") == "TIL"


def test_section_folders():
    r = ranker()
    assert r.section_folder(f"{ORIGIN}/blog/intro") == "blog"
    assert r.section_folder(f"{ORIGIN}/blog/2024/intro") == "blog"
    # Docs are grouped one level deeper; pages right under /docs/ stay one section, so a
    # flat folder (or a link trap in it) is still capped as one.
    assert r.section_folder(f"{ORIGIN}/docs/web/x") == "docs/web"
    assert r.section_folder(f"{ORIGIN}/docs/web") == "docs"
    assert r.section_folder(f"{ORIGIN}/docs") is None
    assert r.section_folder(f"{ORIGIN}/pricing") is None  # top-level: the general section
    assert r.section_folder(f"{ORIGIN}/2024/05/post") is None  # a year is no heading
    assert r.section_folder(f"{ORIGIN}/optional/x") is None  # would clash with ## Optional


def test_low_value_pages_match_first_or_shallow_last_folder():
    r = ranker()
    assert r.is_low_value(f"{ORIGIN}/privacy-policy")
    assert r.is_low_value(f"{ORIGIN}/legal/anything")
    assert r.is_low_value(f"{ORIGIN}/about/careers")
    assert r.is_low_value(f"{ORIGIN}/privacy-security")  # policy word inside the name
    assert r.is_low_value(f"{ORIGIN}/accessibility-policy")
    assert not r.is_low_value(f"{ORIGIN}/docs/web/privacy")  # deep content about privacy
    assert not r.is_low_value(f"{ORIGIN}/docs/privacy-features")
    assert not r.is_low_value(f"{ORIGIN}/laws-and-legal-issues")
    for content in ("legal-services", "cookie-recipes", "careers-advice"):
        assert not r.is_low_value(f"{ORIGIN}/{content}"), content
    assert r.is_low_value(f"{ORIGIN}/cookie-preferences")
    assert r.is_low_value(f"{ORIGIN}/terms-and-conditions")


def test_sections_group_merge_and_put_optional_last():
    pages = ["/docs/a", "/docs/b", "/blog/x", "/pricing", "/privacy", "/terms"]
    assert sections_of(pages, inlinks={f"{ORIGIN}/docs/a": 3}) == [
        ("Docs", ["/docs/a", "/docs/b"]),
        # /blog has one page, so it joins the general section, in rank order.
        ("Pages", ["/pricing", "/blog/x"]),
        ("Optional", ["/privacy", "/terms"]),
    ]


def test_landing_page_leads_its_section():
    pages = ["/docs/a", "/docs/b", "/docs", "/pricing"]
    assert sections_of(pages, inlinks={f"{ORIGIN}/docs/a": 5}) == [
        ("Docs", ["/docs", "/docs/a", "/docs/b"]),
        ("Pages", ["/pricing"]),
    ]


def test_same_titled_folders_merge_into_one_section():
    pages = ["/about/a", "/about/b", "/about-acme/c", "/about-acme/d"]
    names = [name for name, _ in sections_of(pages)]
    assert names == ["About"]


def test_optional_folder_never_makes_a_second_optional_section():
    pages = ["/optional/a", "/optional/b", "/privacy"]
    assert [name for name, _ in sections_of(pages)] == ["Pages", "Optional"]


def test_long_general_section_splits_by_homepage_navigation(monkeypatch):
    monkeypatch.setattr(rank_module, "MAX_PAGES_PER_SECTION", 3)
    pages = ["/a", "/b", "/c", "/d", "/e"]
    assert sections_of(pages, home_links=["/a", "/c"]) == [
        ("Main pages", ["/a", "/c"]),
        ("More pages", ["/b", "/d", "/e"]),
    ]


def test_version_copies_of_a_page_are_listed_once():
    pages = ["/docs/intro", "/docs/2.x/intro", "/docs/3.0.1/intro", "/docs/setup"]
    assert sections_of(pages) == [("Docs", ["/docs/intro", "/docs/setup"])]


def test_caps_number_of_pages(monkeypatch):
    monkeypatch.setattr(rank_module, "MAX_PAGES", 3)
    monkeypatch.setattr(rank_module, "MAX_OPTIONAL_PAGES", 1)
    pages = [f"/docs/p{n}" for n in range(10)] + ["/privacy", "/terms"]
    assert [len(ps) for _, ps in sections_of(pages)] == [3, 1]


def test_one_section_cannot_crowd_out_the_rest(monkeypatch):
    monkeypatch.setattr(rank_module, "MAX_PAGES", 6)
    monkeypatch.setattr(rank_module, "MAX_PAGES_PER_SECTION", 2)
    features = [f"/features/f{n}" for n in range(10)]  # boosted, so they'd fill every slot
    sections = dict(sections_of([*features, "/blog/a", "/blog/b", "/about", "/team"]))
    assert len(sections["Features"]) == 2
    assert len(sections["Blog"]) == 2
    assert len(sections["Pages"]) == 2  # top-level pages fill the rest


def test_deeper_grouping_names_sections_by_the_second_folder():
    layout = SiteLayout(dominant_folder="docs")
    pages = ["/docs/web/a", "/docs/web/b", "/docs/glossary", "/docs/glossary/x",
             "/docs/glossary/y", "/about", "/blog/x", "/blog/y",
             "/docs/cli", "/docs/seo"]  # fmt: skip
    assert sections_of(pages, layout=layout) == [
        ("Pages", ["/about"]),  # sections are ordered by their best page
        ("Docs", ["/docs/cli", "/docs/seo"]),  # pages right under the dominant folder
        ("Glossary", ["/docs/glossary", "/docs/glossary/x", "/docs/glossary/y"]),  # landing first
        ("Web", ["/docs/web/a", "/docs/web/b"]),
        ("Blog", ["/blog/x", "/blog/y"]),  # other folders still group at the first level
    ]


def test_version_folders_never_name_a_section():
    pages = ["/sdk/2.x/a", "/sdk/2.x/b"]
    assert ranker().section_folder(f"{ORIGIN}/sdk/2.x/a") == "sdk"
    assert [name for name, _ in sections_of(pages)] == ["SDK"]


def test_dominant_folder_is_capped_in_total(monkeypatch):
    monkeypatch.setattr(rank_module, "MAX_PAGES", 7)  # a full file: the cap makes room
    monkeypatch.setattr(rank_module, "MAX_DEEP_FOLDER_PAGES", 4)
    layout = SiteLayout(dominant_folder="wiki")  # not docs, so it has no budget of its own
    wiki = [f"/wiki/{area}/p{n}" for area in "abc" for n in range(3)]
    pages = [*wiki, "/blog/x", "/blog/y", "/about"]
    listed = [url for _, urls in sections_of(pages, layout=layout) for url in urls]
    assert sum(url.startswith("/wiki/") for url in listed) == 4
    assert {"/blog/x", "/blog/y", "/about"} <= set(listed)


def test_pick_spread_reaches_small_sections_then_fills_the_budget():
    r = ranker()
    big = [f"{ORIGIN}/components/c{n}" for n in range(20)]  # ranked first
    small = [f"{ORIGIN}/{area}/{page}" for area in ("layout", "forms", "utilities")
             for page in ("x", "y")]  # fmt: skip
    picked = r.pick_spread([*big, *small], 10, 10)
    assert set(small) <= set(picked)  # each section got its turns first
    assert len(picked) == 10  # and the rest of the budget was filled


def test_pick_spread_gives_a_lone_page_its_turn_with_its_parent_section():
    r = ranker()
    web = [f"{ORIGIN}/docs/web/{page}" for page in "abcd"]  # an area: "docs/web"
    leaves = [f"{ORIGIN}/docs/config", f"{ORIGIN}/docs/deploy"]  # one page each: "docs"
    assert r.pick_spread([*web, *leaves], 4, 4) == [web[0], leaves[0], web[1], leaves[1]]


def test_old_versions_and_listing_pages_only_fill_what_is_left():
    r = ranker()
    current = [f"{ORIGIN}/docs/web/{page}" for page in "abc"]
    demoted = [f"{ORIGIN}/docs/2.x/old/a", f"{ORIGIN}/docs/2.x/old/b",
               f"{ORIGIN}/tags/x", f"{ORIGIN}/tags/y"]  # fmt: skip
    assert r.pick_spread([*demoted, *current], 3, 3) == current
    assert len(r.pick_spread([*demoted, *current], 5, 5)) == 5


def test_dominant_cap_gives_slots_back_when_nothing_else_needs_them(monkeypatch):
    monkeypatch.setattr(rank_module, "MAX_DEEP_FOLDER_PAGES", 4)
    layout = SiteLayout(dominant_folder="docs")
    docs = [f"/docs/{area}/p{n}" for area in "abc" for n in range(3)]
    listed = [url for _, urls in sections_of(docs, layout=layout) for url in urls]
    assert len(listed) == 9  # an all-docs site still lists all of them


def test_policy_pages_with_extensions_brand_names_or_in_other_languages():
    r = ranker(layout=SiteLayout(brand_words=frozenset({"spiegel"})))
    for path in ["/privacy-notice.html", "/terms-of-use.html", "/web-accessibility.html",
                 "/datenschutz-spiegel", "/nutzungsbedingungen", "/barrierefreiheit", "/agb",
                 "/mentions-l%C3%A9gales", "/aviso-legal", "/aup", "/dpa",
                 "/legal/acceptable-use-policy", "/help/terms-conditions"]:  # fmt: skip
        assert r.is_low_value(ORIGIN + path), path
    for path in ["/spiegel", "/stories.html", "/docs/privacy-features", "/datenschutz-tools/x",
                 "/tags/privacy.html"]:  # fmt: skip
        assert not r.is_low_value(ORIGIN + path), path


def test_job_postings_are_capped_in_optional():
    jobs = [f"/careers/{n:08x}-1111-2222-3333-444444444444" for n in range(5)]
    sections = dict(sections_of(["/careers", *jobs, "/privacy", "/a", "/b"]))
    assert sections["Optional"] == ["/careers", "/privacy", jobs[0], jobs[1]]


def test_listing_pages_are_capped_and_dated_posts_fill_in_but_only_a_few():
    years = [f"/{year}/" for year in range(2001, 2011)]
    tags = [f"/tags/t{n}/" for n in range(6)]
    posts = [f"/2026/nov/{day}/post-{day}/" for day in range(1, 11)]
    listed = [url for _, urls in sections_of([*years, *tags, *posts, "/about"]) for url in urls]
    assert sum(url.startswith("/tags/") for url in listed) == rank_module.MAX_LISTING_PAGES
    assert not set(years) & set(listed)  # a date archive ranks below other listings
    assert sum(url in posts for url in listed) == rank_module.MAX_DATED_PAGES


def test_docs_get_first_claim_on_the_budget(monkeypatch):
    monkeypatch.setattr(rank_module, "MAX_PAGES", 6)
    monkeypatch.setattr(rank_module, "MAX_DOCS_PAGES", 3)
    marketing = [f"/product-{n}" for n in range(10)]
    docs = ["/docs/start", "/docs/api/a", "/docs/api/b"]
    # Shallow marketing pages linked from every page outrank the docs...
    inlinks = {ORIGIN + url: 10 for url in marketing}
    listed = [url for _, urls in sections_of([*marketing, *docs], inlinks=inlinks) for url in urls]
    # ...but the docs get their budget first, and the rest of the site gets the rest.
    assert set(docs) <= set(listed) and len(listed) == 6


def test_docs_areas_take_turns_at_every_level(monkeypatch):
    monkeypatch.setattr(rank_module, "MAX_DOCS_PAGES", 4)
    monkeypatch.setattr(rank_module, "MAX_PAGES", 4)
    guides = [f"/docs/guides/{area}/p{n}" for area in ("auth", "storage") for n in range(5)]
    reference = [f"/docs/reference/{lang}/p{n}" for lang in ("js", "py") for n in range(5)]
    # The guides rank first, but each area at each level gets a turn before any gets two.
    inlinks = {ORIGIN + url: 5 for url in guides}
    listed = {u for _, urls in sections_of([*guides, *reference], inlinks=inlinks) for u in urls}
    assert {url.split("/")[3] for url in listed} == {"auth", "storage", "js", "py"}


def test_a_lone_docs_page_joins_docs_and_the_docs_landing_heads_its_own_section():
    pages = ["/docs", "/docs/web/a", "/docs/web/b", "/docs/css/a", "/docs/css/b", "/team"]
    assert sections_of(pages) == [
        ("Docs", ["/docs"]),  # alone, but its areas are sections of their own
        ("CSS", ["/docs/css/a", "/docs/css/b"]),
        ("Web", ["/docs/web/a", "/docs/web/b"]),
        ("Pages", ["/team"]),
    ]
    assert sections_of(["/docs/cli", "/docs/seo", "/docs/web/a", "/docs/web/b"]) == [
        ("Docs", ["/docs/cli", "/docs/seo"]),  # one-page areas join their parent
        ("Web", ["/docs/web/a", "/docs/web/b"]),
    ]


def test_fetching_skips_turns_for_folders_nothing_links_to():
    linked = [f"{ORIGIN}/products/p{n}" for n in range(5)]
    records = [f"{ORIGIN}/records/r{n}" for n in range(5)]
    r = ranker(inlinks={f"{ORIGIN}/products/p0": 1})
    # The records rank first (discovery order), but only linked folders take turns.
    picked = r.pick_to_fetch([*records, *linked], 5)
    assert picked == [*linked]
    # With nothing linked at all (an app's empty homepage), every folder takes turns.
    assert len(set(ranker().pick_to_fetch([*records, *linked], 4)) & set(records)) == 2
