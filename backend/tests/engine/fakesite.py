"""A fake website for engine tests: no network, no real DNS."""

import httpx2

from app.engine.fetch import Fetcher

PUBLIC_IP = "93.184.215.14"  # any global address; the SSRF guard lets it through


def page(title=None, description=None, links=(), head="", body=""):
    """A small HTML page."""
    parts = ["<html><head>"]
    if title is not None:
        parts.append(f"<title>{title}</title>")
    if description is not None:
        parts.append(f'<meta name="description" content="{description}">')
    parts.append(head)
    parts.append("</head><body>")
    parts += [f'<a href="{href}">link</a>' for href in links]
    parts.append(body)
    parts.append("</body></html>")
    return "".join(parts)


def urlset(*urls):
    locs = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return xml(f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>')


def sitemapindex(*urls):
    locs = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
    return xml(
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</sitemapindex>'
    )


def xml(body):
    return (200, {"content-type": "application/xml"}, body)


def text(body):
    return (200, {"content-type": "text/plain"}, body)


def redirect(location, status=301):
    return (status, {"location": location}, "")


class FakeSite:
    """Routes map a full URL to an HTML string or a (status, headers, body) tuple.

    A route may also be a list of those, served in turn (the last one repeats), to fake a
    page that fails once and then works. Unknown URLs get a 404. `requests` records every
    URL fetched, in order. `dns` maps host names to IPs; anything else is PUBLIC_IP.
    """

    def __init__(self, routes, dns=None):
        self.routes = {url: list(r) if isinstance(r, list) else [r] for url, r in routes.items()}
        self.dns = dns or {}
        self.requests = []

    def handler(self, request):
        url = str(request.url)
        self.requests.append(url)
        responses = self.routes.get(url)
        if not responses:
            return httpx2.Response(404)
        route = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(route, str):
            return httpx2.Response(200, html=route)
        status, headers, body = route
        return httpx2.Response(
            status, headers=headers, content=body if isinstance(body, bytes) else body.encode()
        )

    async def resolve(self, host):
        return [self.dns.get(host, PUBLIC_IP)]

    def fetcher(self):
        """Build inside a running event loop (the test's asyncio.run)."""
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        return Fetcher(client, self.resolve)
