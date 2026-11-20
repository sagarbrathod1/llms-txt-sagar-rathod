"""Caps on the work the public API accepts. There is no auth, so anyone can make the service crawl.

- Per client: generate jobs per client in the last hour, so one script submitting in a loop
  can't fill the queue and make everyone else wait.
- Monitored sites: daily rechecks are standing load that grows with every site enrolled, and
  nothing ever removes a site.

Neither cap is a budget control. Pinning the database's compute to its smallest size is what
keeps the two-week review window inside the free allowance even if the database never sleeps
(14 days x 24 h x 0.25 CU = 84 of 100 CU-hours). Awake for a whole month, it would run out
around day 17: accepted for a take-home.

The counts live in Postgres (jobs.client_key, sites.monitor_enabled), not in memory, because
the API machine stops when idle and restarts on the next request: an in-memory count would
reset every time.
"""

import ipaddress
import math

from fastapi import HTTPException, Request

# Generate jobs per client per hour (the window is queries.SUBMISSION_WINDOW_SECONDS). Generous
# for a person trying out many sites (a crawl takes about a minute, so 20 an hour is more than
# anyone clicks through by hand), small enough that one client can't fill the queue alone.
PER_CLIENT_LIMIT = 20

# Sites re-checked daily. Each check is about a minute of crawling, so 100 sites add ~100
# crawl-minutes a day: a few minutes per hourly drain, which keeps monitoring on schedule.
MAX_MONITORED_SITES = 100


def check_submission_rate(recent: dict) -> None:
    """Raise a 429 if this client has used up its submissions for the hour.

    `recent` is queries.count_recent_submissions. The count is read before the job is
    inserted, so two simultaneous submissions can both pass at the limit and overshoot it by
    one: harmless, and it avoids serializing every submission.
    """
    if recent["count"] < PER_CLIENT_LIMIT:
        return
    wait = recent["retry_after"]
    # Retry-After takes whole seconds; round up so retrying on time doesn't hit the limit again.
    retry_after = max(1, math.ceil(wait))
    minutes = math.ceil(retry_after / 60)
    raise HTTPException(
        status_code=429,
        detail=f"You've submitted {PER_CLIENT_LIMIT} sites in the last hour. "
        f"Please try again in {minutes} minute{'' if minutes == 1 else 's'}.",
        headers={"Retry-After": str(retry_after)},
    )


def monitoring_at_capacity() -> str:
    """The 409 message for PATCH. The frontend shows it after "Could not update monitoring:"."""
    return (
        f"Monitoring is at capacity ({MAX_MONITORED_SITES} sites). This site's llms.txt still "
        "works, but it won't be re-checked automatically. Generate it again to refresh it."
    )


def client_ip(request: Request) -> str:
    """The submitting client's IP address.

    On Fly, every request arrives through Fly's proxy, which sets Fly-Client-IP to the address
    that connected to it (replacing any value the client sent), and the machine isn't reachable
    except through that proxy. So the header can be trusted there. Without it, as in local
    development, the TCP peer address is the client. Behind any other proxy this would need
    revisiting: the header could then be forged.
    """
    return request.headers.get("fly-client-ip") or (
        request.client.host if request.client else "unknown"
    )


def client_key(ip: str) -> str:
    """What the per-client limit counts by: an IPv4 address, or an IPv6 /64 network.

    An IPv6 subscriber typically gets a whole /64 and can use any address in it, so limiting
    single IPv6 addresses would limit nothing. Stored on the job (jobs.client_key) and used
    for nothing but this rate limit.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip  # not an IP (e.g. a test client's placeholder host): count it as given
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped  # ::ffff:1.2.3.4 is the IPv4 client 1.2.3.4
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)
