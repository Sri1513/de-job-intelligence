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
