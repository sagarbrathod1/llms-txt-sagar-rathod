"""The /sites and /jobs endpoints. The full submit-generate-serve flow is in test_workflows.py."""

import uuid

import pytest

from app import queries


def test_post_returns_the_active_job_instead_of_queueing_another(client, db):
    first = client.post("/sites", json={"url": "example.com"}).json()
    second = client.post("/sites", json={"url": "example.com"}).json()
    assert second == first
    assert len(db.sql("SELECT id FROM jobs")) == 1


def test_post_normalizes_url_to_one_site(client):
    a = client.post("/sites", json={"url": "Example.com/docs?x=1"}).json()
    b = client.post("/sites", json={"url": "https://example.com"}).json()
    assert a["site_id"] == b["site_id"]
    assert client.get(f"/sites/{a['site_id']}").json()["url"] == "https://example.com"


def test_post_rejects_invalid_url_with_readable_message(client, db):
    # Which inputs are invalid is tested with normalize_origin (tests/engine/test_urls.py).
    response = client.post("/sites", json={"url": "localhost"})
    assert response.status_code == 422
    assert "valid website address" in response.json()["detail"]  # a message, not pydantic's list
    assert db.sql("SELECT id FROM sites") == []


def test_post_leaves_monitoring_settings_alone(client, db):
    # "Generate again" must not undo the user's choice to turn monitoring off.
    site_id = client.post("/sites", json={"url": "example.com"}).json()["site_id"]
    db.sql("UPDATE jobs SET status = 'failed'")
    db.sql(
        "UPDATE sites SET consecutive_failures = 3, monitor_enabled = false WHERE id = %s",
        (site_id,),
    )
    assert client.post("/sites", json={"url": "example.com"}).status_code == 202
    site = client.get(f"/sites/{site_id}").json()
    assert site["monitor_enabled"] is False
    assert site["consecutive_failures"] == 3


# The race in queries.create_site_and_enqueue: the INSERT hits the one-active-job index, but
# that job finishes before the lookup that should return it. These fake the lookup to force it.


def test_post_retries_when_active_job_finishes_between_statements(client, monkeypatch):
    first = client.post("/sites", json={"url": "example.com"}).json()
    real_lookup = queries.get_active_job_id
    calls = []

    async def job_finishes_first_time(conn, site_id):
        calls.append(site_id)
        if len(calls) == 1:
            # The active job finishes right after our INSERT hit the conflict: the lookup
            # finds nothing, and the retried INSERT now succeeds.
            await conn.execute(
                "UPDATE jobs SET status = 'succeeded' WHERE site_id = %s", (site_id,)
            )
            return None
        return await real_lookup(conn, site_id)

    monkeypatch.setattr(queries, "get_active_job_id", job_finishes_first_time)
    second = client.post("/sites", json={"url": "example.com"})
    assert second.status_code == 202
    assert second.json()["job_id"] != first["job_id"]
    assert len(calls) == 1


def test_post_returns_503_when_both_tries_lose_the_race(client, monkeypatch):
    client.post("/sites", json={"url": "example.com"})

    async def never_found(conn, site_id):
        return None

    monkeypatch.setattr(queries, "get_active_job_id", never_found)
    response = client.post("/sites", json={"url": "example.com"})
    assert response.status_code == 503
    assert "try again" in response.json()["detail"]


def test_version_is_only_served_under_its_own_site(client, db):
    site_a = client.post("/sites", json={"url": "a.com"}).json()["site_id"]
    site_b = client.post("/sites", json={"url": "b.com"}).json()["site_id"]
    job_id = db.sql("SELECT id FROM jobs WHERE site_id = %s", (site_a,))[0]["id"]
    version_id = db.sql(
        "INSERT INTO llms_versions (site_id, job_id, content, content_sha256) "
        "VALUES (%s, %s, '# A', 'sha') RETURNING id",
        (site_a, job_id),
    )[0]["id"]

    assert client.get(f"/sites/{site_a}/versions/{version_id}").status_code == 200
    assert client.get(f"/sites/{site_b}/versions/{version_id}").status_code == 404


def test_patch_toggles_monitoring_and_re_enabling_resets_failures(client, db):
    site_id = client.post("/sites", json={"url": "example.com"}).json()["site_id"]
    db.sql("UPDATE sites SET consecutive_failures = 3 WHERE id = %s", (site_id,))

    response = client.patch(f"/sites/{site_id}", json={"monitor_enabled": False})
    assert response.status_code == 200
    assert response.json()["monitor_enabled"] is False

    response = client.patch(f"/sites/{site_id}", json={"monitor_enabled": True})
    assert response.json()["monitor_enabled"] is True
    assert response.json()["consecutive_failures"] == 0


UNKNOWN = uuid.uuid4()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"/jobs/{UNKNOWN}"),
        ("GET", f"/sites/{UNKNOWN}"),
        ("GET", f"/sites/{UNKNOWN}/llms.txt"),
        ("GET", f"/sites/{UNKNOWN}/versions/{UNKNOWN}"),
        ("PATCH", f"/sites/{UNKNOWN}"),
    ],
)
def test_unknown_ids_are_404(client, method, path):
    response = client.request(method, path, json={"monitor_enabled": True})
    assert response.status_code == 404
