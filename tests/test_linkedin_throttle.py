# tests/test_linkedin_throttle.py
"""Tests for LinkedIn request throttling.

Contract: LinkedIn 429s burst request patterns even from a residential IP
(verified 2026-10-16: single guest-API requests return 200 while pipeline
bursts 429). Throttling inserts small jittered pauses: before each guest-API
fetch, between JobSpy site scrapes, and between JIT widening boards. Every
delay is env-overridable per call site; 0 disables.
"""
import os
from unittest.mock import MagicMock, call, patch

import pandas as pd

from src.engine import scraper
from src.engine.jit_enrichment import enrich_job_description
from src.engine.scraper import (
    fetch_linkedin_guest_job,
    fetch_scraped_jobs,
    linkedin_throttle_sleep,
)

GOOD_HTML = """
<html><body>
<h2 class="top-card-layout__title">Senior Data Engineer</h2>
<a class="topcard__org-name-link">Acme Corp</a>
<span class="topcard__flavor--bullet">Remote</span>
<div class="show-more-less-html__markup">We are hiring a Senior Data Engineer to build
and operate our Databricks lakehouse platform. You will design scalable ETL pipelines
in PySpark and partner with analytics teams on reporting and data quality.</div>
</body></html>
"""


def _resp(status=200, text=GOOD_HTML):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


# --- linkedin_throttle_sleep unit behavior ---


def test_throttle_uses_default_plus_jitter(monkeypatch):
    monkeypatch.delenv("LINKEDIN_TEST_DELAY_S", raising=False)
    with patch.object(scraper.time, "sleep") as mock_sleep, patch.object(
        scraper.random, "uniform", return_value=0.5
    ):
        linkedin_throttle_sleep("LINKEDIN_TEST_DELAY_S", 5.0, jitter_s=1.5)
    mock_sleep.assert_called_once_with(5.5)


def test_throttle_env_override(monkeypatch):
    monkeypatch.setenv("LINKEDIN_TEST_DELAY_S", "0.2")
    with patch.object(scraper.time, "sleep") as mock_sleep, patch.object(
        scraper.random, "uniform", return_value=0.0
    ):
        linkedin_throttle_sleep("LINKEDIN_TEST_DELAY_S", 5.0)
    mock_sleep.assert_called_once_with(0.2)


def test_throttle_disabled_when_zero(monkeypatch):
    monkeypatch.setenv("LINKEDIN_TEST_DELAY_S", "0")
    with patch.object(scraper.time, "sleep") as mock_sleep:
        linkedin_throttle_sleep("LINKEDIN_TEST_DELAY_S", 5.0)
    mock_sleep.assert_not_called()


def test_throttle_garbage_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LINKEDIN_TEST_DELAY_S", "not-a-number")
    with patch.object(scraper.time, "sleep") as mock_sleep, patch.object(
        scraper.random, "uniform", return_value=0.0
    ):
        linkedin_throttle_sleep("LINKEDIN_TEST_DELAY_S", 5.0)
    mock_sleep.assert_called_once_with(5.0)


# --- call-site wiring ---


def test_guest_fetch_throttles_before_request(monkeypatch):
    """The throttle fires before the HTTP request, every call."""
    manager = MagicMock()
    monkeypatch.setattr(scraper, "linkedin_throttle_sleep", manager.throttle)
    monkeypatch.setattr(scraper.requests, "get", manager.get)
    manager.get.return_value = _resp()
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)

    job = fetch_linkedin_guest_job("12345")

    assert job is not None
    assert job["title"] == "Senior Data Engineer"
    manager.throttle.assert_called_once_with("LINKEDIN_GUEST_DELAY_S", 5.0)
    # throttle must precede the request
    throttle_idx = next(
        i for i, c in enumerate(manager.mock_calls) if c[0] == "throttle"
    )
    get_idx = next(i for i, c in enumerate(manager.mock_calls) if c[0] == "get")
    assert throttle_idx < get_idx


def test_scraped_jobs_sleeps_between_sites_not_before_first(monkeypatch):
    df = pd.DataFrame([{"title": "Data Engineer", "company": "Acme"}])
    monkeypatch.setattr(scraper, "scrape_jobs", lambda **kw: df)
    with patch.object(scraper, "linkedin_throttle_sleep") as mock_throttle:
        monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
        jobs = fetch_scraped_jobs(
            search_term="Data Engineer", site_name=["linkedin", "indeed"]
        )
    assert len(jobs) == 2
    mock_throttle.assert_called_once_with("LINKEDIN_SITE_DELAY_S", 3.0)


def test_widening_sleeps_between_boards(monkeypatch):
    """JIT widening pauses between board attempts instead of burst-firing."""
    import src.engine.jit_enrichment as jit

    monkeypatch.setattr(jit, "fetch_scraped_jobs", lambda **kw: [])
    with patch.object(jit, "linkedin_throttle_sleep") as mock_throttle:
        # job_url=None -> no linkedin_id -> guest API skipped; all boards miss
        result = enrich_job_description("Data Engineer", "Acme Corp", None)
    assert result is None
    assert mock_throttle.call_count == 2  # 3 boards -> 2 gaps
    mock_throttle.assert_has_calls(
        [call("LINKEDIN_WIDEN_DELAY_S", 4.0), call("LINKEDIN_WIDEN_DELAY_S", 4.0)]
    )
