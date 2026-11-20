"""End-to-end workflows through the public interfaces: the HTTP API and the worker's drain.

Only the crawl engine is faked, so these run the API, the queue, the worker and the SQL
together, the way the deployed app does.
"""

import asyncio

from app.db import create_pool
from app.worker import loop


def run_worker() -> dict:
    """One drain, as the hourly scheduled machine runs it. Returns how its jobs ended."""

    async def drain():
        async with create_pool() as pool:
            return await loop.drain(pool)

    # TestClient runs the app's event loop in its own thread, so this one doesn't clash.
    return asyncio.run(drain())


def test_submit_generate_and_serve(client, fake_engine):
    created = client.post("/sites", json={"url": "example.com"})
    assert created.status_code == 202
    site_id, job_id = created.json()["site_id"], created.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()
    assert (job["kind"], job["status"]) == ("generate", "queued")
    assert client.get(f"/sites/{site_id}/llms.txt").status_code == 404  # nothing generated yet

    fake_engine("# Example\n")
    assert run_worker() == {"succeeded": 1}

    assert client.get(f"/jobs/{job_id}").json()["status"] == "succeeded"
    site = client.get(f"/sites/{site_id}").json()
    assert site["latest_job"]["id"] == job_id
    assert site["latest_version"]["content"] == "# Example\n"
    assert len(site["versions"]) == 1
    assert site["next_check_at"] is not None  # monitoring has started
    response = client.get(f"/sites/{site_id}/llms.txt")
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "# Example\n"

    # "Generate again" once the first job is done queues a new job for the same site.
    again = client.post("/sites", json={"url": "example.com"}).json()
    assert again["site_id"] == site_id
    assert again["job_id"] != job_id


def test_monitoring_stores_a_version_only_when_the_site_changed(client, db, fake_engine):
    site_id = client.post("/sites", json={"url": "example.com"}).json()["site_id"]
    fake_engine("# A\n")
    run_worker()

    def recheck(content: str, **result) -> dict:
        db.make_due(site_id)  # a day passes
        fake_engine(content, **result)
        assert run_worker() == {"succeeded": 1}
        return client.get(f"/sites/{site_id}").json()

    site = recheck("# A\n")  # nothing changed: the normal outcome, so no version and no warning
    assert site["latest_job"]["kind"] == "recheck"
    assert site["latest_job"]["warnings"] == []
    assert len(site["versions"]) == 1

    site = recheck("# B\n")  # the site changed
    assert len(site["versions"]) == 2
    assert client.get(f"/sites/{site_id}/llms.txt").text == "# B\n"
    older = site["versions"][1]["id"]  # newest first; the older one stays readable
    assert client.get(f"/sites/{site_id}/versions/{older}").json()["content"] == "# A\n"

    # 2 of 10 pages failed: maybe an outage rather than a change, so no version, and the job
    # says why.
    site = recheck("# B, partly\n", pages=8, failed=2)
    assert len(site["versions"]) == 2
    assert "2 of 10 pages" in site["latest_job"]["warnings"][0]
