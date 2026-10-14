# tests/test_scraper.py
"""Unit tests for the JobSpy scrape wrapper (recency filter + fallback)."""
from datetime import date, timedelta

import pandas as pd
import pytest

from src.engine import scraper


def _rows(dates):
    return pd.DataFrame([
        {"title": f"Data Engineer {i}", "company": "Acme",
         "date_posted": d.isoformat() if d else None}
        for i, d in enumerate(dates)
    ])


def test_fresh_rows_pass_through(monkeypatch, caplog):
    today = date.today()
    monkeypatch.setattr(scraper, "scrape_jobs",
                        lambda **kw: _rows([today, today - timedelta(days=1)]))
    with caplog.at_level("INFO", logger="src.engine.scraper"):
        jobs = scraper.fetch_scraped_jobs(hours_old=48, results_wanted=5,
                                         site_name=["linkedin"])
    assert len(jobs) == 2
    assert not any("older than" in r.message for r in caplog.records)


def test_all_stale_rows_fall_back_to_most_recent(monkeypatch, caplog):
    today = date.today()
    monkeypatch.setattr(
        scraper, "scrape_jobs",
        lambda **kw: _rows([today - timedelta(days=10),
                            today - timedelta(days=4),
                            today - timedelta(days=30)]),
    )
    with caplog.at_level("WARNING", logger="src.engine.scraper"):
        jobs = scraper.fetch_scraped_jobs(hours_old=48, results_wanted=5,
                                         site_name=["linkedin"])
    # Most recent first: 4d, 10d, 30d
    assert [j["date_posted"] for j in jobs] == [
        (today - timedelta(days=4)).isoformat(),
        (today - timedelta(days=10)).isoformat(),
        (today - timedelta(days=30)).isoformat(),
    ]
    assert any("older than 48h" in r.message for r in caplog.records)


def test_empty_scrape_stays_empty(monkeypatch):
    monkeypatch.setattr(scraper, "scrape_jobs", lambda **kw: pd.DataFrame())
    jobs = scraper.fetch_scraped_jobs(hours_old=48, site_name=["linkedin"])
    assert jobs == []


def test_blacklist_still_applies_after_fallback(monkeypatch):
    today = date.today()
    df = pd.DataFrame([
        {"title": "Data Center Technician", "company": "Acme",
         "date_posted": (today - timedelta(days=9)).isoformat()},
        {"title": "Data Engineer", "company": "Acme",
         "date_posted": (today - timedelta(days=9)).isoformat()},
    ])
    monkeypatch.setattr(scraper, "scrape_jobs", lambda **kw: df)
    jobs = scraper.fetch_scraped_jobs(hours_old=48, site_name=["linkedin"])
    assert [j["title"] for j in jobs] == ["Data Engineer"]


_GUEST_HTML = """
<html><body>
  <h2 class="top-card-layout__title">Senior Data Engineer</h2>
  <a class="topcard__org-name-link">Colas USA</a>
  <span class="topcard__flavor--bullet">Tampa, FL, United States</span>
  <div class="show-more-less-html__markup">
    <p>We are hiring a Senior Data Engineer to build and operate our Databricks
    Lakehouse platform. You will design scalable ETL pipelines in PySpark,
    model data in Delta Lake, and partner with analytics teams.</p>
  </div>
</body></html>
"""


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def test_guest_api_parses_posting(monkeypatch):
    monkeypatch.setattr(
        scraper.requests, "get",
        lambda url, **kw: _Resp(200, _GUEST_HTML),
    )
    job = scraper.fetch_linkedin_guest_job("999")
    assert job is not None
    assert job["title"] == "Senior Data Engineer"
    assert job["company"] == "Colas USA"
    assert "Databricks" in job["description"]
    assert job["job_url"] == "https://www.linkedin.com/jobs/view/999/"


def test_guest_api_429_returns_none(monkeypatch):
    monkeypatch.setattr(
        scraper.requests, "get", lambda url, **kw: _Resp(429, "")
    )
    assert scraper.fetch_linkedin_guest_job("999") is None


def test_guest_api_auth_wall_returns_none(monkeypatch):
    # 200 with a login-wall page: no description markup -> None, not garbage.
    monkeypatch.setattr(
        scraper.requests, "get",
        lambda url, **kw: _Resp(200, "<html><body>Join now to apply</body></html>"),
    )
    assert scraper.fetch_linkedin_guest_job("999") is None


def test_guest_api_network_error_returns_none(monkeypatch):
    def _boom(url, **kw):
        raise scraper.requests.RequestException("conn reset")

    monkeypatch.setattr(scraper.requests, "get", _boom)
    assert scraper.fetch_linkedin_guest_job("999") is None


def test_guest_api_empty_id_returns_none():
    assert scraper.fetch_linkedin_guest_job("") is None
    assert scraper.fetch_linkedin_guest_job(None) is None
