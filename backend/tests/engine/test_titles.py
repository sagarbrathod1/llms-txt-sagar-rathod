import pytest

from app.engine.models import PageInfo
from app.engine.titles import (
    add_path_hints,
    choose_site_name,
    page_title,
    shared_title_parts,
    site_name_from_title,
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


def test_shared_part_never_empties_a_title():
    shared = {"mdn"}
    assert page_title("MDN", "Mozilla", f"{ORIGIN}/x", shared) == "MDN"


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
