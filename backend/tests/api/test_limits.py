import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import limits
from app.api.main import app

# --- the decision (pure) ---


def test_under_the_limit_is_accepted():
    limits.check_submission_rate({"count": 19, "retry_after": 5})  # doesn't raise


@pytest.mark.parametrize(
    ("wait", "text", "retry_after"),
    [
        (5, "1 minute", 5),
        (1799.2, "30 minutes", 1800),  # Retry-After rounded up, so retrying on time works
    ],
)
def test_per_client_limit(wait, text, retry_after):
    with pytest.raises(HTTPException) as caught:
        limits.check_submission_rate({"count": 20, "retry_after": wait})
    assert caught.value.status_code == 429
    assert "20 sites in the last hour" in caught.value.detail
    assert f"try again in {text}." in caught.value.detail
    assert caught.value.headers["Retry-After"] == str(retry_after)


# --- who the client is ---


@pytest.mark.parametrize(
    ("headers", "ip"),
    [
        ({"Fly-Client-IP": "1.2.3.4"}, "1.2.3.4"),  # set by Fly's proxy, so trusted
        ({}, "5.6.7.8"),  # no proxy (local development): the TCP peer
    ],
)
def test_client_ip(headers, ip):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    request = Request({"type": "http", "headers": raw_headers, "client": ("5.6.7.8", 1234)})
    assert limits.client_ip(request) == ip


@pytest.mark.parametrize(
    ("ip", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),  # a subscriber's whole /64
        ("::ffff:203.0.113.7", "203.0.113.7"),  # IPv4-mapped is the IPv4 client
        ("testclient", "testclient"),  # not an IP: used as given
    ],
)
def test_client_key(ip, key):
    assert limits.client_key(ip) == key


# --- POST /sites, counted in Postgres ---


@pytest.fixture
def small_limits(monkeypatch):
    # Small limits keep the tests fast; the decision is covered above with the real ones.
    monkeypatch.setattr(limits, "PER_CLIENT_LIMIT", 2)


def post(client, url, ip="1.1.1.1"):
    return client.post("/sites", json={"url": url}, headers={"Fly-Client-IP": ip})


def test_per_client_limit_survives_a_restart_and_slides(db, small_limits):
    """Regression: the limit was once counted in memory, and the API machine restarts on
    every idle cycle, so it reset all the time. Now it is counted in Postgres."""
    with TestClient(app) as client:
        assert post(client, "a.com").status_code == 202
        assert post(client, "b.com").status_code == 202
    with TestClient(app) as client:  # a fresh process
        response = post(client, "c.com")
        assert response.status_code == 429
        assert 3500 < int(response.headers["Retry-After"]) <= 3600
        assert post(client, "c.com", ip="2.2.2.2").status_code == 202  # other clients unaffected

        db.sql("UPDATE jobs SET created_at = now() - interval '61 minutes'")
        assert post(client, "d.com").status_code == 202  # only the last hour counts


def test_only_created_jobs_count(client, small_limits):
    for _ in range(5):  # one job; the rest return it
        assert post(client, "a.com").status_code == 202
    for _ in range(3):
        assert post(client, "localhost").status_code == 422  # invalid: nothing created
    assert post(client, "b.com").status_code == 202
    assert post(client, "c.com").status_code == 429


def test_monitoring_cap(client, monkeypatch):
    monkeypatch.setattr(limits, "MAX_MONITORED_SITES", 2)
    a = post(client, "a.com", ip="10.0.0.1").json()["site_id"]
    post(client, "b.com", ip="10.0.0.2")
    c = post(client, "c.com", ip="10.0.0.3").json()["site_id"]

    # Over the cap, a new site is still created and generated, just not monitored.
    assert client.get(f"/sites/{a}").json()["monitor_enabled"] is True
    assert client.get(f"/sites/{c}").json()["monitor_enabled"] is False

    response = client.patch(f"/sites/{c}", json={"monitor_enabled": True})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("Monitoring is at capacity (2 sites)")

    # Re-enabling an already monitored site is harmless; turning one off frees a slot.
    assert client.patch(f"/sites/{a}", json={"monitor_enabled": True}).status_code == 200
    assert client.patch(f"/sites/{a}", json={"monitor_enabled": False}).status_code == 200
    assert client.patch(f"/sites/{c}", json={"monitor_enabled": True}).status_code == 200
