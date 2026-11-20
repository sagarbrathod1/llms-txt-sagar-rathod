"""URL helpers shared by the engine and the API.

normalize_origin is the single definition of site identity: the API stores its result in
sites.url (UNIQUE), so two inputs that mean the same site must normalize to the same string.
"""

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(raw: str) -> str:
    """Turn user input like "Example.com/docs" into an origin like "https://example.com".

    Raises ValueError with a message that is safe to show the user.
    """
    text = raw.strip()
    # Users type "example.com" without a scheme. Checking for "://" rather than trusting
    # urlsplit: urlsplit("example.com:8080") would read "example.com" as the scheme.
    if "://" not in text:
        text = "https://" + text

    parts = urlsplit(text)
    if parts.scheme not in DEFAULT_PORTS:
        raise ValueError("Only http:// and https:// URLs are supported.")
    # Credentials aren't part of an origin, and silently dropping them would turn odd input
    # like "mailto:me@example.com" into a site the user didn't mean.
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs with a username or password aren't supported.")

    # .hostname is already lowercased by urlsplit. A trailing dot ("example.com.") names the
    # same host, so drop it to keep one row per site.
    host = (parts.hostname or "").rstrip(".")
    labels = host.split(".")
    # A dotted hostname rules out "localhost", typos like "examplecom", and empty input.
    # Characters are checked here so bad input gets a readable error now, not a DNS failure later.
    if len(labels) < 2 or not all(labels) or not all(ch.isalnum() or ch in "-." for ch in host):
        raise ValueError(f"{raw.strip()!r} isn't a valid website address, e.g. example.com.")

    try:
        port = parts.port
    except ValueError:  # urlsplit raises on a non-numeric or out-of-range port
        raise ValueError("The port in that URL isn't valid.") from None

    # www. is deliberately kept: www.example.com can serve a different site than example.com.
    origin = f"{parts.scheme}://{host}"
    if port is not None and port != DEFAULT_PORTS[parts.scheme]:
        origin += f":{port}"
    return origin


# Query parameters that only track where a visitor came from. Two URLs differing only in these
# are the same page, so they're dropped before deduplication.
TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga", "_hsenc", "_hsmi"}

# Links to files rather than pages. Skipping them by extension saves a request; anything that
# slips through is still rejected later by its Content-Type.
NON_PAGE_EXTENSIONS = {
    ".7z", ".avi", ".css", ".csv", ".doc", ".docx", ".dmg", ".exe", ".gif", ".gz", ".ico",
    ".jpeg", ".jpg", ".js", ".json", ".m4a", ".mov", ".mp3", ".mp4", ".pdf", ".png", ".ppt",
    ".pptx", ".rar", ".rss", ".svg", ".tar", ".tgz", ".txt", ".wav", ".webm", ".webp", ".woff",
    ".woff2", ".xls", ".xlsx", ".xml", ".zip",
}  # fmt: skip


def origin_of(url: str) -> str | None:
    """The normalized origin of an absolute http(s) URL, or None if it has no valid one."""
    try:
        if urlsplit(url).scheme not in DEFAULT_PORTS:
            return None
        return normalize_origin(url)
    except ValueError:  # e.g. "http://[not-an-ip]/": urlsplit rejects malformed hosts
        return None


def normalize_page_url(href: str, base: str, origin: str) -> str | None:
    """Resolve a link found on `base` into a canonical page URL on `origin`, or None to skip it.

    Skips other origins, non-http(s) schemes (mailto:, javascript:) and links to files.
    Drops the fragment, tracking parameters and a trailing index.html, sorts the query, and
    maps http:// and www. variants onto `origin`, so the same page isn't counted twice.
    """
    # Links on real pages can be malformed ("http://[your-server]/api"); urljoin and urlsplit
    # raise ValueError for those, and one bad link must not end the whole run.
    try:
        url = urljoin(base, href.strip())
        parts = urlsplit(url)
    except ValueError:
        return None
    link_origin = origin_of(url)
    # http:// and www. variants of the site's own host are the same site: links to them are
    # rewritten onto `origin` below. Any other host, or another port, is a different site.
    if link_origin is None or _site_key(link_origin) != _site_key(origin):
        return None

    path = parts.path or "/"
    last_segment = path.rsplit("/", 1)[-1].lower()
    if "." in last_segment and "." + last_segment.rsplit(".", 1)[-1] in NON_PAGE_EXTENSIONS:
        return None
    # Web servers serve /docs/index.html for /docs/, so both name one page.
    if last_segment in ("index.html", "index.htm"):
        path = path[: -len(last_segment)]

    # Sorted, so ?a=1&b=2 and ?b=2&a=1 are one page.
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.startswith("utm_") and key not in TRACKING_PARAMS
        )
    )
    # Rebuilt from `origin` so host case, default ports and trailing dots match it exactly.
    return urlunsplit(urlsplit(origin)[:2] + (path, query, ""))


def _site_key(origin: str) -> str:
    """ "https://www.example.com" -> "example.com": the host and port, ignoring scheme and www."""
    return origin.split("://", 1)[1].removeprefix("www.")
