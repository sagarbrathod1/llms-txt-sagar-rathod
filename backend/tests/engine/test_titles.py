import pytest

from app.engine.models import PageInfo
from app.engine.titles import (
    add_path_hints,
    choose_site_name,
    page_title,
    shared_title_parts,
    site_name_from_title,
    title_from_path,
)

ORIGIN = "https://example.com"


@pytest.mark.parametrize(
    ("title", "hostname", "expected"),
    [
        ("Acme | Widgets for everyone", "acme.com", "Acme"),
        ("Home - Acme", "acme.com", "Acme"),
        # A trailing "Home" goes only when the rest is the domain's name.
        ("BBC Home | Breaking News, World News", "www.bbc.com", "BBC"),
        ("Zara Home | Official Website", "www.zarahome.com", "Zara Home"),
        ("Smith Funeral Home", "smithfuneralhome.com", "Smith Funeral Home"),
        # The same rule for a leading "Home".
        ("Home Acme | Widgets for everyone", "acme.com", "Acme"),
        ("Home Chef | Meal Kit Delivery", "www.homechef.com", "Home Chef"),
        ("Welcome to GOV.UK", "www.gov.uk", "GOV.UK"),  # "Welcome to" always goes
        ("</> htmx - high power tools for html", "htmx.org", "htmx"),  # decorative symbols go
        ("Svelte • Web development for the rest of us", "svelte.dev", "Svelte"),  # "•" splits
        ("Black & Decker", "blackanddecker.com", "Black & Decker"),  # a symbol inside is kept
        ("Acme", "acme.com", "Acme"),
        (None, "acme.com", None),
    ],
)
def test_site_name_from_title(title, hostname, expected):
    assert site_name_from_title(title, hostname) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Pricing | Acme", "Pricing"),
        ("Acme - Pricing", "Pricing"),
        ("Acme ~ Pricing", "Pricing"),
        ("Pricing • Acme", "Pricing"),
        ("</> Acme ~ Pricing", "Pricing"),  # "</> Acme" is the site name all the same
        ("</> Pricing", "Pricing"),  # symbol-only prefixes go
        ("</>", "Getting started"),
        ("Pricing and plans", "Pricing and plans"),
        ("Acme", "Getting started"),  # only the site name: fall back to the URL
        (None, "Getting started"),
    ],
)
def test_page_title(title, expected):
    assert page_title(title, "Acme", f"{ORIGIN}/docs/getting-started") == expected


def test_page_title_strips_repeated_and_curly_apostrophe_site_names():
    name = "Zingerman's Deli"
    title = "Events - Zingerman’s Deli - Zingerman’s Deli"
    assert page_title(title, name, f"{ORIGIN}/events") == "Events"
    # The title's own separators survive when nothing is stripped.
    assert page_title("A - B | C", "Acme", f"{ORIGIN}/x") == "A - B | C"


def test_shared_title_parts_are_stripped():
    titles = [
        "Quickstart — Requests 2.34.2 documentation",
        "Advanced Usage — Requests 2.34.2 documentation",
        "API — Requests 2.34.2 documentation",
        "Requests: HTTP for Humans",
    ]
    shared = shared_title_parts(titles)
    assert shared == {"requests 2.34.2 documentation"}
    assert page_title(titles[0], "Requests", f"{ORIGIN}/q", shared) == "Quickstart"


def test_few_titles_are_not_enough_evidence():
    assert shared_title_parts(["A | MDN", "B | MDN"]) == set()


def test_a_page_titled_like_the_homepage_falls_back_to_the_path():
    """Regression (Patagonia): pages titled only with the homepage's title were listed as
    "Patagonia Outdoor Clothing & Gear (Womens)"."""
    home = "Patagonia Outdoor Clothing & Gear"
    assert page_title(home, "Patagonia", f"{ORIGIN}/shop/womens", home_title=home) == "Womens"
    sitemap = page_title(f"Sitemap | {home}", "Patagonia", f"{ORIGIN}/sitemap/", home_title=home)
    assert sitemap == "Sitemap"
    # Codes at the end of the path give way to the words before them.
    product = f"{ORIGIN}/product/the-wolverine-way-paperback-book/BK212.html"
    assert (
        page_title(home, "Patagonia", product, home_title=home)
        == "The wolverine way paperback book"
    )


def test_a_title_that_is_only_a_shared_part_is_kept():
    # The landing page of the section whose pages all end in "| Python Software Foundation".
    assert page_title("MDN", "Mozilla", f"{ORIGIN}/x", {"mdn"}) == "MDN"


def test_duplicate_titles_get_a_path_hint():
    def info(path, title):
        return PageInfo(f"{ORIGIN}{path}", title, None, "Docs")

    sections = [("Docs", [info("/docs/2.x/intro", "Introduction"),
                          info("/docs/3.0.1/intro", "Introduction"),
                          info("/docs/setup", "Setup")])]  # fmt: skip

    def segments(url):
        return [s for s in url.removeprefix(ORIGIN).split("/") if s]

    hinted = add_path_hints(sections, segments)
    assert [p.title for p in hinted[0][1]] == [
        "Introduction (2.x)",
        "Introduction (3.0.1)",
        "Setup",
    ]


def test_path_hints_skip_id_segments():
    """Regression (Linear): job posts got hints like "(069c4628 88d7 4e4d b393 ...)"."""

    def info(path, title):
        return PageInfo(f"{ORIGIN}{path}", title, None, "Careers")

    uuid = "069c4628-88d7-4e4d-b393-c996fc7f3076"
    pages = [
        info(f"/careers/{uuid}", "Engineer"),
        info("/careers/d3bc1ced-3ce4-4086-a050-555055dbb1ff", "Engineer"),
        info("/news/1030441/permitting-bill", "Story"),
        info("/news/1038597/senate-vote", "Story"),
        info("/2024/recap", "Recap"),
        info("/2025/recap", "Recap"),
    ]
    sections = [("Careers", pages)]

    def segments(url):
        return [s for s in url.removeprefix(ORIGIN).split("/") if s]

    titles = [p.title for p in add_path_hints(sections, segments)[0][1]]
    assert titles == [
        "Engineer",  # nothing but the ID tells these apart: no hint beats a UUID
        "Engineer",
        "Story (Permitting bill)",  # the slug after a numeric ID does
        "Story (Senate vote)",
        "Recap (2024)",  # years aren't IDs
        "Recap (2025)",
    ]


def test_shared_parts_are_stripped_layer_by_layer():
    titles = [f"{name} | Django documentation | Django" for name in ("Models", "Views", "Forms")]
    titles += ["Writing your first app | Django documentation | Django"]
    shared = shared_title_parts(titles)
    assert shared == {"django", "django documentation"}
    assert page_title(titles[0], "Django Project", f"{ORIGIN}/m", shared) == "Models"


def test_path_hint_that_repeats_the_title_is_dropped():
    def info(path, title):
        return PageInfo(f"{ORIGIN}{path}", title, None, "Docs")

    sections = [("Docs", [info("/a/customer-requests", "Customer Requests"),
                          info("/b/x", "Customer Requests")])]  # fmt: skip

    def segments(url):
        return [s for s in url.removeprefix(ORIGIN).split("/") if s]

    titles = [p.title for p in add_path_hints(sections, segments)[0][1]]
    assert titles == ["Customer Requests (A)", "Customer Requests (B)"]
    sections = [("Docs", [info("/customer-requests", "Customer Requests"),
                          info("/other", "Customer Requests")])]  # fmt: skip
    titles = [p.title for p in add_path_hints(sections, segments)[0][1]]
    assert titles == ["Customer Requests", "Customer Requests (Other)"]


@pytest.mark.parametrize(
    ("og", "alternatives", "title", "expected"),
    [
        ("Acme", ["Other"], "Long tagline here", "Acme"),  # og:site_name wins
        (None, ["Acme App"], "Welcome | Acme", "Acme App"),  # application-name / JSON-LD
        (None, [], "Welcome to GOV.UK", "GOV.UK"),
        (None, [], "Home - Acme", "Acme"),
        # A tagline isn't a name: use the hostname.
        (None, [], "Latest news, sport and opinion from the Guardian", "theguardian.com"),
        (None, ["The Guardian"], "Latest news, sport and opinion from the Guardian",
         "The Guardian"),
    ],
)  # fmt: skip
def test_choose_site_name(og, alternatives, title, expected):
    assert choose_site_name(og, alternatives, title, "www.theguardian.com") == expected


def test_og_site_name_that_is_just_the_hostname_is_skipped():
    """Regression (fastht.ml): og:site_name "fastht.ml" made the H1 the hostname."""
    title = "FastHTML - Modern web applications in pure Python"
    assert choose_site_name("fastht.ml", [], title, "fastht.ml") == "FastHTML"
    assert choose_site_name("www.Acme.com", [], "Acme | Widgets", "www.acme.com") == "Acme"
    assert choose_site_name("fastht.ml", [], None, "fastht.ml") == "fastht.ml"


def test_a_page_titled_exactly_like_the_homepage_falls_back_even_without_the_site_name():
    """Regression: an app shell titled "Acme • The app for widgets" with og:site_name "Acme Inc"
    gave "Acme (Dashboard)" while the warning said titles came from the URLs."""
    shell = "Acme • The app for widgets"
    shared = {"acme", "the app for widgets"}  # every shell page carries both parts
    url = f"{ORIGIN}/dashboard"
    assert page_title(shell, "Acme Inc", url, shared, home_title=shell) == "Dashboard"
    # Content pages that all reuse the homepage's title: what's left is boilerplate too.
    reused = "Acme | Widgets for everyone"
    url = f"{ORIGIN}/about"
    assert page_title(reused, "Acme", url, {"widgets for everyone"}, home_title=reused) == "About"
    # ...but what's left after the site name still names the page (semver.org/spec).
    spec = "Semantic Versioning 2.0.0 | Semantic Versioning"
    url = f"{ORIGIN}/spec/v2.0.0.html"
    assert (
        page_title(spec, "Semantic Versioning", url, home_title=spec) == "Semantic Versioning 2.0.0"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/solutions/ai", "Ai"),  # short words without digits are words
        ("/changelog/page/2", "Page"),  # a page number is a code
        ("/product/wolverine-way/BK212.html", "Wolverine way"),  # so is a SKU
        ("/careers/069c4628-88d7-4e4d-b393-c996fc7f3076", "Careers"),  # and a UUID
    ],
)
def test_path_titles_skip_codes_but_not_short_words(path, expected):
    assert title_from_path(f"{ORIGIN}{path}") == expected
