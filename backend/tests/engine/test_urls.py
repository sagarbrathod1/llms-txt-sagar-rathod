import pytest

from app.engine.urls import normalize_origin, normalize_page_url, origin_of

# --- normalize_origin: site identity (sites.url is UNIQUE on its result) ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com"),  # scheme added
        ("  HTTPS://Example.COM/docs?x=1#top ", "https://example.com"),  # case, path, spaces
        ("http://example.com", "http://example.com"),  # explicit http kept
        ("https://example.com:443", "https://example.com"),  # default port dropped
        ("http://example.com:443", "http://example.com:443"),  # 443 isn't http's default
        ("example.com:8080", "https://example.com:8080"),  # urlsplit would read a scheme here
        ("https://www.example.com", "https://www.example.com"),  # www. can be another site
        ("https://example.com.", "https://example.com"),  # trailing dot is the same host
    ],
)
def test_normalize_origin(raw, expected):
    assert normalize_origin(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "valid website address"),
        ("localhost", "valid website address"),  # not dotted
        ("https://example..com", "valid website address"),  # empty label
        ("https://exa mple.com", "valid website address"),  # invalid character
        ("ftp://example.com", "http:// and https://"),
        ("https://user:pass@example.com", "username or password"),
        ("mailto:me@example.com", "username or password"),  # not silently a site "mailto"
        ("https://example.com:99999", "port"),
    ],
)
def test_normalize_origin_rejects_with_a_readable_message(raw, message):
    with pytest.raises(ValueError, match=message):
        normalize_origin(raw)


# --- normalize_page_url: one URL per page, for deduplication ---

ORIGIN = "https://example.com"
BASE = "https://example.com/docs/intro"


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/pricing", "https://example.com/pricing"),
        ("setup", "https://example.com/docs/setup"),  # relative to the page
        ("https://EXAMPLE.com:443/a", "https://example.com/a"),  # host case, default port
        ("/a#section", "https://example.com/a"),  # fragment dropped
        ("/a?utm_source=x&id=7&gclid=1", "https://example.com/a?id=7"),  # tracking dropped
        ("https://example.com", "https://example.com/"),  # empty path is "/"
        ("/docs/index.html", "https://example.com/docs/"),  # directory index is the directory
        ("/a?b=2&a=1", "https://example.com/a?a=1&b=2"),  # query sorted
        ("http://example.com/a", "https://example.com/a"),  # http variant of our site
        ("https://www.example.com/a", "https://example.com/a"),  # www variant of our site
        ("  /spaced  ", "https://example.com/spaced"),
    ],
)
def test_normalizes_page_urls(href, expected):
    assert normalize_page_url(href, BASE, ORIGIN) == expected


@pytest.mark.parametrize(
    "href",
    [
        "https://other.com/a",  # other site
        "https://docs.example.com/a",  # subdomain is another origin
        "https://example.com:8443/a",  # another port is another site
        "mailto:hi@example.com",
        "javascript:void(0)",
        "/files/report.PDF",  # files, not pages
        # Regression: malformed hosts on real pages made urlsplit raise and ended the run.
        "http://[your-server]/api",
        "http://[::1/x",
    ],
)
def test_skips_non_pages_and_other_origins(href):
    assert normalize_page_url(href, BASE, ORIGIN) is None


def test_origin_of():
    assert origin_of("https://Example.com/a/b?c") == "https://example.com"
    assert origin_of("ftp://example.com/") is None
    assert origin_of("https://localhost/") is None
