"""The worker's command line (app.worker.main), as the hourly scheduled machine runs it."""

import logging

import pytest

from app.worker import loop
from app.worker import main as worker_main


def test_drain_command_runs_the_queue_and_exits_0(db, fake_engine, caplog):
    db.add_site(job_kind="generate")
    fake_engine()

    # A sync test: main() runs its own event loop, as `python -m app.worker.main --drain` does.
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exit_info:
        worker_main.main(["--drain"])

    assert exit_info.value.code == 0
    assert "Drain finished: 1 succeeded, 0 failed, 0 discarded, 0 errored" in caplog.text


def test_drain_command_exits_1_after_errors(db_url, monkeypatch):
    # Non-zero, so a drain that couldn't work shows up as a failed run in the host's logs.
    async def tick(pool):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(loop, "tick", tick)
    with pytest.raises(SystemExit) as exit_info:
        worker_main.main(["--drain"])
    assert exit_info.value.code == 1
