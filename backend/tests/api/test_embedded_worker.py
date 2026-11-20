"""EMBEDDED_WORKER=true: the API process runs the job loop for as long as it is up."""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import engine
from app.api.main import app
from app.config import settings


@pytest.fixture
def embedded(db_url, monkeypatch):
    monkeypatch.setattr(settings, "embedded_worker", True)


def test_startup_runs_the_loop_which_submissions_wake(embedded, db, fake_engine):
    fake_engine("# Example\n")
    with TestClient(app) as client:
        time.sleep(0.3)  # let the startup tick finish
        # Queued behind the API's back: no wake-up, and the loop doesn't poll (so the database
        # can suspend), so it waits.
        _, quiet_job = db.add_site("https://quiet.com", job_kind="generate")
        time.sleep(0.5)
        assert db.job(quiet_job)["status"] == "queued"

        # A submission wakes the loop, which then runs jobs back-to-back until none are left.
        created = client.post("/sites", json={"url": "example.com"}).json()
        deadline = time.monotonic() + 5
        while {db.job(j)["status"] for j in (quiet_job, created["job_id"])} != {"succeeded"}:
            assert time.monotonic() < deadline, "the woken loop didn't run both jobs"
            time.sleep(0.05)
        assert client.get(f"/sites/{created['site_id']}/llms.txt").text == "# Example\n"


def test_shutdown_hands_running_job_back_to_queue(embedded, db, monkeypatch):
    # threading.Event: the app runs on TestClient's event loop in another thread.
    started = threading.Event()

    async def slow(url, on_progress=None):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(engine, "generate_llms_txt", slow)
    with TestClient(app) as client:
        job_id = client.post("/sites", json={"url": "example.com"}).json()["job_id"]
        assert started.wait(timeout=5), "the embedded worker didn't start the job"
    # Leaving the block shut the app down, mid-crawl.

    job = db.job(job_id)
    assert (job["status"], job["attempts"], job["locked_at"]) == ("queued", 1, None)
