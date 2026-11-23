# tests/test_dashboard_lockscreen.py
"""Tests for the dynamic countdown lock screen (/lockscreen).

Public GET: one saved job + an ISO deadline with a timezone offset renders
the countdown page. Unknown job -> 404; missing or naive deadline -> 400;
flag off -> 404. No schema change, no MCP tool: the deadline is a param.
"""
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard.app import app


def _mock_db(monkeypatch, row):
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    monkeypatch.setattr("src.dashboard.app.get_db_connection", lambda: cm)


def _job_row(**overrides):
    row = {
        "job_id": "li-4474501423",
        "title": "Senior Data Engineer II",
        "company": "Pax8",
        "location": "USA",
        "fit_score": 63,
    }
    row.update(overrides)
    return row


@pytest.fixture(autouse=True)
def _enable_lockscreen(monkeypatch):
    monkeypatch.setattr(settings, "LOCKSCREEN_ENABLED", True)


def test_lockscreen_renders_job_and_deadline(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 200
    assert "Pax8 — Senior Data Engineer II" in resp.text
    assert "2026-11-30T23:59:00-05:00" in resp.text
    assert "Nov 30, 2026" in resp.text
    # Nine milestones derive from the deadline: six steps (minus
    # 16/14/13/13/6/3 days) plus three sub-steps under the tailored
    # package, all Nov 17 (minus 13 days).
    assert "Nov 14" in resp.text
    assert "Nov 16" in resp.text
    assert "Nov 17" in resp.text
    assert "Nov 24" in resp.text
    assert "Nov 27" in resp.text
    assert "Nov 18" not in resp.text
    assert "Nov 20" not in resp.text
    for label in (
        "Job saved",
        "Gate passed",
        "Tailored package built",
        "Resume tailored",
        "Cover letter drafted",
        "Outreach draft ready",
        "Application submitted",
        "Follow up if no receipt",
        "Reply expected",
    ):
        assert label in resp.text
    # The three package sub-steps render indented under their parent.
    assert "mile done sub" in resp.text
    # Dream-job design: mountain photo bg + responsive scaler.
    assert "/static/lockscreen-bg.webp" in resp.text
    assert 'id="scaler"' in resp.text


def test_lockscreen_top_safe_zone_clears_phone_clock(monkeypatch):
    # Real-phone check (2026-11-22): a 148px clock-space let the phone's
    # big lock-screen clock + date (~200px) cover the eyebrow and title.
    # 250px cleared it; the owner then saw empty space above the eyebrow
    # and asked for the panel higher, so the zone is 230px — still clears
    # a ~200px clock (30px at scale 1, ~18px at 1.06x).
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 200
    assert ".clock-space { height: 215px" in resp.text


def test_lockscreen_body_bg_is_full_bleed_photo(monkeypatch):
    # Wide screens (tablet, desktop): the 390px card only fills the middle,
    # so the body's photo must show on the sides — not a near-black overlay
    # (the old .88 left black bars on the tablet, 2026-11-22; the veil was
    # halved to .22 on 2026-11-23 because it dulled the photo between tiles).
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 200
    assert "rgba(6,11,14,.22) 100%)" in resp.text
    assert "rgba(6,11,14,.88)" not in resp.text
    assert "rgba(6,11,14,.45) 100%)" not in resp.text


def test_lockscreen_bg_option(monkeypatch):
    # ?bg= picks the background (glow default, mist, fog); an unknown
    # name falls back to the default instead of 400ing.
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    base = "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    assert "lockscreen-bg.webp" in client.get(base).text
    assert "lockscreen-bg-mist.jpg" in client.get(base + "&bg=mist").text
    assert "lockscreen-bg-fog.webp" in client.get(base + "&bg=fog").text
    assert "lockscreen-bg-forest.jpg" in client.get(base + "&bg=forest").text
    assert "lockscreen-bg-rain.jpg" in client.get(base + "&bg=rain").text
    assert "lockscreen-bg.webp" in client.get(base + "&bg=nope").text


def test_lockscreen_bg_image_serves(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    for bg_file in (
        "/static/lockscreen-bg.webp",
        "/static/lockscreen-bg-mist.jpg",
        "/static/lockscreen-bg-fog.webp",
        "/static/lockscreen-bg-forest.jpg",
        "/static/lockscreen-bg-rain.jpg",
    ):
        resp = client.get(bg_file)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/")


def test_lockscreen_unknown_job_404(monkeypatch):
    _mock_db(monkeypatch, None)
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-nope&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 404


def test_lockscreen_missing_deadline_400(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get("/lockscreen?job_id=li-4474501423")
    assert resp.status_code == 400


def test_lockscreen_naive_deadline_400(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00"
    )
    assert resp.status_code == 400


def test_lockscreen_plus_offset_arrives_as_space(monkeypatch):
    # An unencoded "+" offset decodes to a space in the query string;
    # the handler must restore it before parsing.
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00+00:00"
    )
    assert resp.status_code == 200
    assert "2026-11-30T23:59:00+00:00" in resp.text


def test_lockscreen_disabled_404(monkeypatch):
    monkeypatch.setattr(settings, "LOCKSCREEN_ENABLED", False)
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 404
