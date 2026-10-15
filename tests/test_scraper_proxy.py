# tests/test_scraper_proxy.py
"""Tests for residential-proxy routing of scraper traffic.

Context: LinkedIn/Indeed/ZipRecruiter 429/403/challenge the VM's datacenter
IP. SCRAPER_PROXY_URL points at a forward proxy on the user's laptop over
Tailscale (residential IP). When the proxy is unreachable the code must
fall back to direct -- today's behavior, never worse.
"""

import os
from unittest.mock import patch

import pytest

from src.engine import scraper
from src.engine.scraper import maybe_scraper_proxy

PROXY = "http://100.68.109.36:8888"
ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("SCRAPER_PROXY_URL", raising=False)
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_no_proxy_url_is_noop():
    with maybe_scraper_proxy():
        for k in ENV_KEYS:
            assert k not in os.environ


def test_unreachable_proxy_falls_back_to_direct():
    os.environ["SCRAPER_PROXY_URL"] = PROXY
    with patch("socket.create_connection", side_effect=OSError("down")):
        with maybe_scraper_proxy():
            for k in ENV_KEYS:
                assert k not in os.environ


def test_malformed_proxy_url_falls_back_to_direct():
    os.environ["SCRAPER_PROXY_URL"] = "not-a-url"
    with maybe_scraper_proxy():
        for k in ENV_KEYS:
            assert k not in os.environ


def test_reachable_proxy_sets_env_and_restores():
    os.environ["SCRAPER_PROXY_URL"] = PROXY
    with patch("socket.create_connection") as mock_conn:
        with maybe_scraper_proxy():
            mock_conn.assert_called_once()
            host, port = mock_conn.call_args.args[0]
            assert (host, port) == ("100.68.109.36", 8888)
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                assert os.environ[k] == PROXY
            assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
        # restored afterwards
        for k in ENV_KEYS:
            assert k not in os.environ


def test_preexisting_env_is_preserved():
    os.environ["SCRAPER_PROXY_URL"] = PROXY
    os.environ["HTTPS_PROXY"] = "http://corporate:3128"
    with patch("socket.create_connection"):
        with maybe_scraper_proxy():
            assert os.environ["HTTPS_PROXY"] == PROXY
        assert os.environ["HTTPS_PROXY"] == "http://corporate:3128"


def test_guest_api_runs_inside_proxy_context():
    # The guest fetch must route through the proxy when it is up.
    entered = []

    real_cm = maybe_scraper_proxy

    def spy_cm():
        entered.append(True)
        return real_cm()

    os.environ["SCRAPER_PROXY_URL"] = PROXY
    fake_resp = type(
        "R",
        (),
        {
            "status_code": 200,
            "text": (
                '<h2 class="top-card-layout__title">Data Engineer</h2>'
                '<div class="show-more-less-html__markup"><p>' + "x" * 200 + "</p></div>"
            ),
        },
    )()
    with (
        patch("socket.create_connection"),
        patch.object(scraper, "maybe_scraper_proxy", side_effect=spy_cm),
        patch.object(scraper.requests, "get", return_value=fake_resp) as mock_get,
    ):
        job = scraper.fetch_linkedin_guest_job("123")
    assert entered, "guest fetch must run inside maybe_scraper_proxy()"
    assert job is not None and len(job["description"]) >= 50
    mock_get.assert_called_once()


def test_scraped_jobs_routes_through_proxy(monkeypatch):
    # fetch_scraped_jobs wraps the site loop in maybe_scraper_proxy, so
    # JobSpy's internal session picks the proxy up from the env.
    import pandas as pd

    os.environ["SCRAPER_PROXY_URL"] = PROXY
    seen_env = {}

    def fake_scrape_jobs(**kwargs):
        seen_env.update({k: os.environ.get(k) for k in ENV_KEYS})
        return pd.DataFrame(
            [
                {
                    "title": "Data Engineer",
                    "company": "Acme",
                    "location": "United States",
                    "job_url": "https://x",
                    "description": "y" * 200,
                    "date_posted": "N/A",
                }
            ]
        )

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape_jobs)
    with patch("socket.create_connection"):
        jobs = scraper.fetch_scraped_jobs(
            search_term="Data Engineer",
            results_wanted=1,
            hours_old=0,
            site_name=["linkedin"],
        )
    assert len(jobs) == 1
    assert seen_env["HTTPS_PROXY"] == PROXY
    assert seen_env["NO_PROXY"] == "localhost,127.0.0.1"
    # env restored after the call
    for k in ENV_KEYS:
        assert k not in os.environ
