"""HTTP fetching with the safety and politeness limits every request needs.

Every request made by the engine goes through Fetcher.get, so the limits below can't be
skipped: SSRF guard on each redirect hop, timeouts, a response-size cap, a concurrency cap,
backing off when the site says we're too fast, and an overall deadline for the run.
"""

import asyncio
import contextlib
import ipaddress
import math
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx2

# Honest and identifiable, so site owners can see who we are and target us in robots.txt.
USER_AGENT = "llms-txt-generator/0.1"
# We only parse HTML pages (sitemaps and robots.txt ignore this header in practice).
ACCEPT = "text/html,application/xhtml+xml"
# Per network operation (connect, each read). A healthy page answers well within this.
TIMEOUT_SECONDS = 10.0
# Whole request including redirects and body. The per-read timeout alone wouldn't stop a
# server that drips one byte every few seconds.
TOTAL_TIMEOUT_SECONDS = 30.0
# Following redirects ourselves so each hop gets the SSRF check. Browsers allow ~20; real
# sites rarely chain more than 2-3, so 5 is generous.
MAX_REDIRECTS = 5
# Polite to the target site and enough to crawl ~100 pages in well under a minute.
CONCURRENCY = 5
# HTML pages are read up to 2 MB and the rest is dropped. Title, description and canonical
# live in <head> at the top and the first paragraphs follow soon after, so a cut-off page is
# still useful (some framework pages inline 10 MB of script data). Parsing less also keeps
# each page's parse fast.
MAX_PAGE_BYTES = 2_000_000
# Long enough for a brief network blip to clear, short enough not to drag.
RETRY_DELAY_SECONDS = 1.0

# Rate limiting. A 429 (or a 503 with Retry-After) pauses *every* request: the site is
# telling us, not about one page. For Retry-After seconds if given, else 5 s doubling while
# the answers keep coming, never over 30 s; any successful page resets it. A 503 without
# Retry-After is usually one broken page, not throttling, so it's just retried.
FIRST_PAUSE_SECONDS = 5.0
MAX_PAUSE_SECONDS = 30.0
# Each 429 also slows the rest of the run: one request at a time, 1 s apart, then 2 s, 4 s.
SLOW_SPACING_SECONDS = 1.0
# A 429 at the slowest spacing means the site won't let us crawl it now; continuing would
# produce a degraded file, so the run gives up. Counted in a row: a success resets it.
MAX_CONSECUTIVE_RATE_LIMITS = 3
# robots.txt Crawl-delay is honored up to this; a site asking for 60 s would never finish.
MAX_CRAWL_DELAY_SECONDS = 5.0

NAT64 = ipaddress.ip_network("64:ff9b::/96")

Resolver = Callable[[str], Awaitable[list[str]]]


class FetchError(Exception):
    """A request that didn't produce a usable response. The message is short and user-safe.

    retryable is True for failures that may succeed on a second try (timeouts, connection
    errors, 5xx, 429); a 404 or a blocked address will not.
    """

    def __init__(
        self,
        message: str,
        retryable: bool = False,
        status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status = status  # the HTTP status, when the server answered with an error
        self.retry_after = retry_after  # seconds, from a Retry-After header


class BlockedAddressError(FetchError):
    """The SSRF guard refused the URL: its host resolves to a private or internal address."""


class RateLimitedError(FetchError):
    """We gave up on this site: it kept answering 429 even after we slowed down."""


class DeadlineError(FetchError):
    """The run's time limit passed, so this request wasn't sent."""


@dataclass
class FetchResult:
    url: str  # final URL, after redirects
    content_type: str  # lowercased media type without parameters, e.g. "text/html"
    charset: str | None
    headers: httpx2.Headers
    body: bytes


async def resolve_host(host: str) -> list[str]:
    """Resolve a hostname to all its IP addresses, using the OS resolver without blocking."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def is_blocked_ip(text: str) -> bool:
    """True for addresses a public website must never point us at."""
    ip = ipaddress.ip_address(text.split("%")[0])  # drop an IPv6 zone like "fe80::1%eth0"
    # "::ffff:10.0.0.1" is an IPv4 address in IPv6 form; judge it as the IPv4 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global is False for private, loopback, link-local, reserved, unspecified and
    # carrier-grade NAT ranges. Multicast is checked on its own because some multicast
    # ranges count as global. NAT64 (64:ff9b::/96) embeds an IPv4 address that a NAT64
    # gateway would connect to, which could be an internal one.
    return not ip.is_global or ip.is_multicast or ip in NAT64


class Fetcher:
    """Fetches URLs with the SSRF guard, limits, politeness and our User-Agent.

    One Fetcher serves one generation run, so its pause, slowdown and deadline state is
    per site. The client and resolver are injected so tests can use httpx2.MockTransport and
    a fake DNS.
    """

    def __init__(self, client: httpx2.AsyncClient, resolve: Resolver = resolve_host):
        self._client = client
        self._resolve = resolve
        self._semaphore = asyncio.Semaphore(CONCURRENCY)
        # When spacing is set (Crawl-delay, or after a 429), requests also take this lock, so
        # they go one at a time with at least `spacing` seconds between their starts.
        self._one_at_a_time = asyncio.Lock()
        self.spacing = 0.0
        self._last_start = -math.inf
        self._resume_at = 0.0  # the shared pause gate: no request starts before this
        self._pause = 0.0  # the last pause length, doubled when the site says "slow down" again
        self._slowed_at = -math.inf  # when a 429 last slowed us down
        self._rate_limits_in_a_row = 0
        self.gave_up = False  # the site kept rate-limiting us; see RateLimitedError
        self._deadline = math.inf

    def start_deadline(self, seconds: float) -> None:
        """No request starts more than `seconds` from now; later ones raise DeadlineError."""
        self._deadline = _now() + seconds

    def seconds_left(self) -> float:
        return self._deadline - _now()

    def set_crawl_delay(self, seconds: float) -> None:
        """Honor robots.txt Crawl-delay: one request at a time, this far apart (capped)."""
        self.spacing = max(self.spacing, min(seconds, MAX_CRAWL_DELAY_SECONDS))

    async def get(self, url: str, *, max_bytes: int, html_only: bool = False) -> FetchResult:
        """Fetch, retrying once after a transient failure so one blip doesn't drop a page."""
        try:
            return await self._attempt(url, max_bytes, html_only)
        except FetchError as error:
            if not error.retryable:
                raise
            # After a 429, or a 503 with Retry-After, the pause gate already makes us wait.
            if not (error.status == 429 or error.retry_after is not None):
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        return await self._attempt(url, max_bytes, html_only)

    async def _attempt(self, url: str, max_bytes: int, html_only: bool) -> FetchResult:
        async with self._semaphore:
            serial = self._one_at_a_time if self.spacing else contextlib.nullcontext()
            async with serial:
                started = await self._wait_for_turn()
                try:
                    async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                        result = await self._get(url, max_bytes, html_only)
                    # The site is answering normally again: the next "slow down" starts over.
                    self._pause = 0.0
                    self._rate_limits_in_a_row = 0
                    return result
                except FetchError as error:
                    if error.status in (429, 503):
                        self._back_off(error, started)
                    raise
                # TimeoutError is our total deadline; TimeoutException is httpx2's own.
                except (TimeoutError, httpx2.TimeoutException):
                    raise FetchError("timed out", retryable=True) from None
                except httpx2.TransportError:
                    raise FetchError("connection failed", retryable=True) from None
                # Anything else the client raises means this URL or response is unusable (a
                # bad redirect Location, undecodable compression, ...). It must cost one page,
                # never the run, and retrying won't help.
                except (httpx2.HTTPError, httpx2.InvalidURL, ValueError):
                    raise FetchError("invalid URL or response") from None

    async def _wait_for_turn(self) -> float:
        """Wait out the pause gate and the spacing; return when this request may start."""
        while True:
            if self.gave_up:
                raise RateLimitedError("the site is rate-limiting our crawler", status=429)
            now = _now()
            start = max(now, self._resume_at, self._last_start + self.spacing)
            if start > self._deadline:
                raise DeadlineError("the time limit for this run was reached")
            if start == now:
                break
            # Loop rather than sleep once: another request may extend the pause meanwhile.
            await asyncio.sleep(start - now)
        self._last_start = _now()
        return self._last_start

    def _back_off(self, error: FetchError, started: float) -> None:
        """The site said "too many requests" (429) or "unavailable" (503): pause everyone."""
        if error.status == 503 and error.retry_after is None:
            return
        if error.retry_after is not None:
            self._pause = min(error.retry_after, MAX_PAUSE_SECONDS)
        else:
            self._pause = min(self._pause * 2 or FIRST_PAUSE_SECONDS, MAX_PAUSE_SECONDS)
        self._resume_at = max(self._resume_at, _now() + self._pause)
        # Only a 429 for a request started after our last slowdown counts: requests already
        # in flight were sent at the old speed and tell us nothing new.
        if error.status != 429 or started < self._slowed_at:
            return
        self._rate_limits_in_a_row += 1
        if self._rate_limits_in_a_row > MAX_CONSECUTIVE_RATE_LIMITS:
            self.gave_up = True
            return
        self.spacing = max(
            self.spacing, SLOW_SPACING_SECONDS * 2 ** (self._rate_limits_in_a_row - 1)
        )  # 1, 2, 4 s
        self._slowed_at = _now()

    async def _get(self, url: str, max_bytes: int, html_only: bool) -> FetchResult:
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_host(url)
            headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.is_redirect and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    # Only the seconds form of Retry-After; the rarer HTTP-date form is ignored.
                    retry_after = response.headers.get("retry-after", "")
                    raise FetchError(
                        f"HTTP {response.status_code}",
                        retryable=True,
                        status=response.status_code,
                        retry_after=float(retry_after) if retry_after.isdigit() else None,
                    )
                if response.status_code != 200:
                    raise FetchError(f"HTTP {response.status_code}", status=response.status_code)

                content_type = response.headers.get("content-type", "")
                media_type = content_type.split(";")[0].strip().lower()
                if html_only and media_type not in ("text/html", "application/xhtml+xml"):
                    raise FetchError(f"not an HTML page ({media_type or 'no content type'})")

                # A cut-off HTML page still has its <head>; a cut-off XML file can't be parsed.
                body = await _read_capped(response, max_bytes, truncate=html_only)
                return FetchResult(
                    url=url,
                    content_type=media_type,
                    charset=response.charset_encoding,
                    headers=response.headers,
                    body=body,
                )
        raise FetchError("too many redirects")

    async def _check_host(self, url: str) -> None:
        """Refuse URLs whose host resolves to a private or internal address (SSRF guard).

        Known gap (DNS rebinding): the HTTP client resolves the name again when it connects,
        so a hostile DNS server could answer differently the second time. Closing it means
        connecting to the IP we checked; out of scope for v1.
        """
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError("not an http(s) URL")
        try:
            addresses = await self._resolve(parts.hostname)
        except OSError:
            # Retryable: for a host that resolved a moment ago, a failed lookup is a DNS blip.
            raise FetchError("couldn't resolve the host name", retryable=True) from None
        # Block if *any* address is internal: the client could connect to any of them.
        if not addresses or any(is_blocked_ip(address) for address in addresses):
            raise BlockedAddressError("blocked: the address is private or internal")


def _now() -> float:
    # The event loop's monotonic clock, so wall-clock changes can't skew pauses or deadlines.
    return asyncio.get_running_loop().time()


async def _read_capped(response: httpx2.Response, max_bytes: int, truncate: bool) -> bytes:
    """Read at most max_bytes of the body (after decompression).

    Beyond that, either keep what was read (truncate) or refuse the response.
    """
    declared = response.headers.get("content-length", "")
    if not truncate and declared.isdigit() and int(declared) > max_bytes:
        raise FetchError("response too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > max_bytes:
            if not truncate:
                raise FetchError("response too large")
            break  # leaving the stream early closes the connection; the rest isn't downloaded
    return b"".join(chunks)[:max_bytes]
