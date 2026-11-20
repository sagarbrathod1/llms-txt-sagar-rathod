import asyncio
import contextlib

import httpx2
import pytest
from fakesite import FakeSite, page, redirect

from app.engine import fetch
from app.engine.fetch import (
    MAX_REDIRECTS,
    BlockedAddressError,
    FetchError,
    is_blocked_ip,
)


def get(site, url, **kwargs):
    async def go():
        return await site.fetcher().get(url, max_bytes=1000, **kwargs)

    return asyncio.run(go())


def timed(site):
    """Record the loop time of every request the site receives."""
    times, inner = [], site.handler

    def handler(request):
        times.append(asyncio.get_running_loop().time())
        return inner(request)

    site.handler = handler
    return times


def run_concurrently(site, urls, setup=None):
    """Fetch urls at once through one Fetcher; return (results or errors, the fetcher)."""

    async def go():
        fetcher = site.fetcher()
        if setup:
            setup(fetcher)
        results = await asyncio.gather(
            *(fetcher.get(url, max_bytes=1000) for url in urls), return_exceptions=True
        )
        return results, fetcher

    return asyncio.run(go())


class InFlight:
    """An async handler wrapper that records the most requests in progress at once."""

    def __init__(self, site, delay=0.01):
        self.site, self.delay, self.now, self.most = site, delay, 0, 0
        self.inner = site.handler
        site.handler = self.handle

    async def handle(self, request):
        self.now += 1
        self.most = max(self.most, self.now)
        await asyncio.sleep(self.delay)
        self.now -= 1
        return self.inner(request)


# --- the SSRF guard ---


@pytest.mark.parametrize(
    ("ip", "blocked"),
    [
        ("10.0.0.1", True), ("172.16.0.1", True), ("192.168.1.1", True),  # private
        ("127.0.0.1", True), ("::1", True),  # loopback
        # link-local (169.254.169.254 is the cloud metadata API)
        ("169.254.169.254", True), ("fe80::1", True),
        ("0.0.0.0", True), ("240.0.0.1", True),  # unspecified, reserved
        ("224.0.0.1", True), ("ff02::1", True),  # multicast
        ("100.64.0.1", True),  # carrier-grade NAT
        ("::ffff:10.0.0.1", True),  # private IPv4 written as IPv6
        # NAT64: blocked whatever IPv4 it embeds (10.0.0.1, 8.8.8.8)
        ("64:ff9b::a00:1", True), ("64:ff9b::808:808", True),
        ("93.184.215.14", False), ("8.8.8.8", False), ("2606:4700::1111", False),  # public
    ],
)  # fmt: skip
def test_is_blocked_ip(ip, blocked):
    assert is_blocked_ip(ip) is blocked


def test_blocks_host_that_resolves_to_private_ip():
    site = FakeSite(
        {"https://intranet.example.com/": page("x")}, dns={"intranet.example.com": "10.1.2.3"}
    )
    with pytest.raises(BlockedAddressError):
        get(site, "https://intranet.example.com/")
    assert site.requests == []  # refused before any request was sent


def test_blocks_redirect_to_private_ip():
    site = FakeSite(
        {
            "https://example.com/": redirect("http://169.254.169.254/latest/meta-data/"),
            "http://169.254.169.254/latest/meta-data/": page("secrets"),
        },
        dns={"169.254.169.254": "169.254.169.254"},
    )
    with pytest.raises(BlockedAddressError):
        get(site, "https://example.com/")
    assert site.requests == ["https://example.com/"]  # the internal hop was never requested


# --- one request: redirects, size, type, retries ---


def test_stops_after_too_many_redirects():
    site = FakeSite({"https://example.com/loop": redirect("/loop")})
    with pytest.raises(FetchError, match="too many redirects"):
        get(site, "https://example.com/loop")
    assert len(site.requests) == MAX_REDIRECTS + 1


def test_refuses_oversized_responses():
    site = FakeSite({"https://example.com/s.xml": (200, {}, "x" * 2000)})
    with pytest.raises(FetchError, match="too large"):
        get(site, "https://example.com/s.xml")


@pytest.mark.parametrize(
    ("responses", "requests", "retryable"),
    [
        ([(503, {}, ""), page("ok")], 2, None),  # a blip: retried once, and it worked
        ([(503, {}, "")], 2, True),  # still failing: given up after one retry
        ([(404, {}, "")], 1, False),  # missing: not retried
    ],
)
def test_retries_a_transient_failure_once(responses, requests, retryable):
    site = FakeSite({"https://example.com/": responses})
    times = timed(site)
    results, fetcher = run_concurrently(site, ["https://example.com/"])
    assert len(site.requests) == requests
    if retryable is None:
        assert not isinstance(results[0], Exception)
    else:
        assert isinstance(results[0], FetchError) and results[0].retryable is retryable
    # A 503 without Retry-After is one page's problem: no slowdown or pause for everyone.
    assert fetcher.spacing == 0
    assert all(b - a < fetch.FIRST_PAUSE_SECONDS for a, b in zip(times, times[1:], strict=False))


def test_retry_after_pauses_the_retry_up_to_a_cap():
    site = FakeSite({"https://example.com/": [(429, {"retry-after": "3600"}, ""), "ok"]})
    times = timed(site)
    get(site, "https://example.com/")
    # Retry-After said an hour; we waited MAX_PAUSE_SECONDS (patched to 0.08 s) instead.
    assert 0.07 <= times[1] - times[0] < 1


def test_html_only_rejects_other_content_types():
    site = FakeSite({"https://example.com/a": (200, {"content-type": "application/pdf"}, "%PDF")})
    with pytest.raises(FetchError, match="not an HTML page"):
        get(site, "https://example.com/a", html_only=True)


def test_sends_our_user_agent_and_asks_for_html():
    # robots.txt rules are matched against this user agent.
    seen = []
    site = FakeSite({"https://example.com/": page("x")})
    original = site.handler

    def handler(request):
        seen.append((request.headers["user-agent"], request.headers["accept"]))
        return original(request)

    site.handler = handler
    get(site, "https://example.com/")
    assert seen == [("llms-txt-generator/0.1", "text/html,application/xhtml+xml")]


@pytest.mark.parametrize(
    "error",
    [httpx2.DecodingError("bad gzip"), httpx2.InvalidURL("bad url"), ValueError("bad")],
)
def test_client_errors_become_non_retryable_fetch_errors(error):
    site = FakeSite({})
    calls = []

    def broken(request):
        calls.append(request)
        raise error

    site.handler = broken
    with pytest.raises(FetchError, match="invalid URL or response") as caught:
        get(site, "https://example.com/")
    assert not caught.value.retryable
    assert len(calls) == 1  # not retried


def test_malformed_redirect_location_is_a_fetch_error():
    site = FakeSite({"https://example.com/": redirect("http://[::1/broken")})
    with pytest.raises(FetchError, match="invalid URL or response"):
        get(site, "https://example.com/")


# --- the crawl as a whole: concurrency, rate limiting (429/503), Crawl-delay ---


def test_concurrency_is_bounded():
    urls = [f"https://example.com/p{n}" for n in range(20)]
    site = FakeSite({url: page("x") for url in urls})
    tracker = InFlight(site)
    run_concurrently(site, urls)
    assert tracker.most == fetch.CONCURRENCY


def test_a_429_pauses_everyone_then_slows_to_one_at_a_time():
    urls = [f"https://example.com/p{n}" for n in range(12)]
    routes = {url: page("x") for url in urls}
    routes[urls[0]] = [(429, {}, ""), page("x")]
    site = FakeSite(routes)
    tracker = InFlight(site)

    async def go():
        fetcher = site.fetcher()
        first = await asyncio.gather(
            *(fetcher.get(url, max_bytes=1000) for url in urls), return_exceptions=True
        )
        tracker.most = 0  # requests started after the 429 must run one at a time
        await asyncio.gather(*(fetcher.get(url, max_bytes=1000) for url in urls[:3]))
        return first, fetcher

    results, fetcher = asyncio.run(go())
    assert all(not isinstance(result, Exception) for result in results)  # the retry worked
    assert fetcher.spacing == fetch.SLOW_SPACING_SECONDS
    assert not fetcher.gave_up
    assert tracker.most == 1


def test_503_with_retry_after_pauses_everyone():
    site = FakeSite({"https://example.com/": [(503, {"retry-after": "1"}, ""), page("ok")]})
    times = timed(site)
    run_concurrently(site, ["https://example.com/"])
    assert times[1] - times[0] >= fetch.MAX_PAUSE_SECONDS * 0.9  # 1 s, capped (patched 0.08)


def test_spacing_doubles_on_each_429_in_a_row_then_gives_up():
    urls = [f"https://example.com/p{n}" for n in range(6)]
    site = FakeSite({url: (429, {}, "") for url in urls})

    async def go():
        fetcher = site.fetcher()
        spacings = []
        for url in urls:  # one at a time, so every request starts after the last slowdown
            with contextlib.suppress(FetchError):
                await fetcher.get(url, max_bytes=1000)
            spacings.append(fetcher.spacing)
        return spacings, fetcher.gave_up

    spacings, gave_up = asyncio.run(go())
    slow = fetch.SLOW_SPACING_SECONDS
    # get() retries once, so each URL is two 429s: 1x and 2x, then 4x, then the 4th in a row.
    assert spacings[:2] == [2 * slow, 4 * slow]
    assert gave_up


def test_429s_separated_by_successes_dont_escalate():
    # Only 429s *in a row* mean the site wants us gone; one now and then is just a nudge.
    urls = [f"https://example.com/p{n}" for n in range(6)]
    site = FakeSite({url: [(429, {}, ""), page("ok")] for url in urls})
    times = timed(site)

    async def go():
        fetcher = site.fetcher()
        for url in urls:  # each one: a 429, then a successful retry
            await fetcher.get(url, max_bytes=1000)
        return fetcher

    fetcher = asyncio.run(go())
    assert fetcher.spacing == fetch.SLOW_SPACING_SECONDS  # slowed once, never doubled
    assert not fetcher.gave_up
    # The pause before the last retry is the first, short one again (patched 0.02 s), not one
    # that kept doubling to the cap (0.08 s).
    assert times[-1] - times[-2] < fetch.MAX_PAUSE_SECONDS / 2


def test_crawl_delay_spaces_requests_one_at_a_time():
    urls = [f"https://example.com/p{n}" for n in range(4)]
    site = FakeSite({url: page("x") for url in urls})
    times = timed(site)
    tracker = InFlight(site, delay=0)
    _, fetcher = run_concurrently(site, urls, setup=lambda f: f.set_crawl_delay(60))
    assert fetcher.spacing == fetch.MAX_CRAWL_DELAY_SECONDS  # 60 s capped (patched to 0.05)
    assert tracker.most == 1
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(gap >= 0.045 for gap in gaps)
