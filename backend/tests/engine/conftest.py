"""Shared setup for engine tests."""

import pytest

from app.engine import fetch


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    # Retries and pauses are tested; waiting real seconds for them isn't. The proportions
    # (first pause < max pause) stay the same so the behavior under test does too.
    monkeypatch.setattr(fetch, "RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(fetch, "FIRST_PAUSE_SECONDS", 0.02)
    monkeypatch.setattr(fetch, "MAX_PAUSE_SECONDS", 0.08)
    monkeypatch.setattr(fetch, "SLOW_SPACING_SECONDS", 0.01)
    monkeypatch.setattr(fetch, "MAX_CRAWL_DELAY_SECONDS", 0.05)
