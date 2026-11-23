# tests/test_dashboard_lockscreen.py
"""Tests for the dynamic countdown lock screen (/lockscreen) and the
current dream-job endpoint (/api/lockscreen/current).

Public GET: one saved job + an optional ISO deadline with a timezone
offset renders the countdown page. An explicit deadline always wins;
when omitted it derives as posted_at + 7 days. Unknown job -> 404;
naive deadline, or omitted deadline with no posted date -> 400; flag
off -> 404. /api/lockscreen/current returns the next dream job in line
under the default ordering rule (open, posted date known, deadline not
passed; highest fit first, earliest derived deadline breaks ties).
"""
from datetime import date
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


def _mock_db_recording(monkeypatch, row):
    """Like _mock_db, but returns the cursor mock so the test can assert
    on the SQL the handler sent (the ordering rule lives in the query,
    and the mock DB never executes it)."""
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    monkeypatch.setattr("src.dashboard.app.get_db_connection", lambda: cm)
    return cur


def _job_row(**overrides):
    row = {
        "job_id": "li-4474501423",
        "title": "Senior Data Engineer II",
        "company": "Pax8",
        "location": "USA",
        "fit_score": 63,
        "posted_at": date(2026, 11, 21),
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
    # Company is the bold primary heading, role on the line below —
    # never joined with an em dash (the old single line wrapped
    # mid-title: "Amazon — Data / Engineer I").
    assert "Pax8" in resp.text
    assert "Senior Data Engineer II" in resp.text
    assert "Pax8 — Senior Data Engineer II" not in resp.text
    assert "2026-11-30T23:59:00-05:00" in resp.text
    assert "Nov 30, 2026" in resp.text
    # Seven milestones derive from the deadline: six steps (minus
    # 16/14/13/13/6/3 days) plus one sub-line under the tailored
    # package, Nov 17 (minus 13 days). The three old sub-steps
    # collapsed into that one line 2026-11-23, and "Gate passed"
    # became "Fit Check Passed" (pipeline jargon judges don't know).
    assert "Nov 14" in resp.text
    assert "Nov 16" in resp.text
    assert "Nov 17" in resp.text
    assert "Nov 24" in resp.text
    assert "Nov 27" in resp.text
    assert "Nov 18" not in resp.text
    assert "Nov 20" not in resp.text
    for label in (
        "Job saved",
        "Fit Check Passed",
        "Tailored package built",
        "Resume · Cover letter · Outreach",
        "Application submitted",
        "Follow up if no receipt",
        "Reply expected",
    ):
        assert label in resp.text
    assert "Gate passed" not in resp.text
    assert "Resume tailored" not in resp.text
    assert "Cover letter drafted" not in resp.text
    assert "Outreach draft ready" not in resp.text
    # The package sub-line renders indented under its parent.
    assert "mile done sub" in resp.text
    # The runway bar spans from the first milestone (job saved, Nov 14)
    # to the deadline, so it shows real progress instead of 0% on day one.
    assert "Nov 14" in resp.text
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


def test_lockscreen_bgurl_custom_image(monkeypatch):
    # ?bgurl= takes any direct https image link and uses it as the
    # background (it overrides the named ?bg= choice). The URL must
    # survive intact — including & in its own query string, so the
    # template renders it |safe after the handler's strict validation.
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    base = "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    custom = (
        "https://images.pexels.com/photos/40990378/pexels-photo-40990378/"
        "free-photo-of-misty-forest-with-evergreen-trees-in-twilight.jpeg"
        "?auto=compress&cs=tinysrgb&w=1260"
    )
    import urllib.parse

    resp = client.get(base + "&bg=mist&bgurl=" + urllib.parse.quote(custom, safe=""))
    assert resp.status_code == 200
    assert custom in resp.text
    assert "&amp;" not in resp.text
    assert "lockscreen-bg-mist.jpg" not in resp.text


def test_lockscreen_fit_hide(monkeypatch):
    # &fit=hide drops the fit score from the title tile (display only —
    # the score stays in the DB row); anything else shows it.
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    base = "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    assert "Fit 63/100" in client.get(base).text
    assert "Fit 63/100" in client.get(base + "&fit=bogus").text
    hidden = client.get(base + "&fit=hide").text
    assert "Fit 63/100" not in hidden
    assert "USA" in hidden


def test_lockscreen_bgurl_rejects_unsafe(monkeypatch):
    # javascript:/http:/quotes/parens all fall back to the default glow
    # background instead of landing in the page.
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    base = "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    import urllib.parse

    for bad in (
        "javascript:alert(1)",
        "http://example.com/x.jpg",
        'https://example.com/x.jpg")',
        "https://example.com/x.jpg');alert(1);//",
    ):
        resp = client.get(base + "&bgurl=" + urllib.parse.quote(bad, safe=""))
        assert resp.status_code == 200
        assert "lockscreen-bg.webp" in resp.text
        assert bad not in resp.text


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


def test_lockscreen_missing_job_id_400(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get("/lockscreen")
    assert resp.status_code == 400


def test_lockscreen_missing_deadline_no_posted_date_400(monkeypatch):
    # Omitted deadline + NULL posted_at -> 400. The saved date is never
    # a substitute (owner decision 2026-11-22).
    _mock_db(monkeypatch, _job_row(posted_at=None))
    client = TestClient(app)
    resp = client.get("/lockscreen?job_id=li-4474501423")
    assert resp.status_code == 400


def test_lockscreen_derived_deadline(monkeypatch):
    # No deadline param: posted 2026-11-21 -> deadline Nov 28, 23:59
    # local. Milestones derive from the effective deadline exactly as
    # from an explicit one: saved Nov 12, fit Nov 14, package Nov 15,
    # follow-up Nov 22, reply Nov 25.
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get("/lockscreen?job_id=li-4474501423")
    assert resp.status_code == 200
    assert "2026-11-28T23:59:00-05:00" in resp.text
    assert "Nov 28, 2026" in resp.text
    for day in ("Nov 12", "Nov 14", "Nov 15", "Nov 22", "Nov 25"):
        assert day in resp.text


def test_lockscreen_explicit_deadline_wins_over_derived(monkeypatch):
    # The row has a posted date, but an explicit deadline beats it —
    # the contest link (Dec 7, not posted + 7) must keep working.
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get(
        "/lockscreen?job_id=li-4474501423&deadline=2026-11-30T23:59:00-05:00"
    )
    assert resp.status_code == 200
    assert "Nov 30, 2026" in resp.text
    assert "Nov 28, 2026" not in resp.text


def test_lockscreen_unknown_job_no_deadline_404(monkeypatch):
    # Unknown job 404s before any derivation is attempted.
    _mock_db(monkeypatch, None)
    client = TestClient(app)
    resp = client.get("/lockscreen?job_id=li-nope")
    assert resp.status_code == 404


def test_api_lockscreen_current_returns_job(monkeypatch):
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get("/api/lockscreen/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "li-4474501423"
    assert body["company"] == "Pax8"
    assert body["title"] == "Senior Data Engineer II"
    assert body["location"] == "USA"
    assert body["fit_score"] == 63
    # Derived deadline: posted Nov 21 + 7 days, end of day local.
    assert body["deadline"] == "2026-11-28T23:59:00-05:00"
    assert body["deadline_display"] == "Nov 28, 2026"
    # The URL carries the job only — the deadline derives server-side.
    assert body["lockscreen_url"] == (
        "http://testserver/lockscreen?job_id=li-4474501423"
    )
    assert "deadline=" not in body["lockscreen_url"]


def test_api_lockscreen_current_query_orders_by_fit_then_deadline(monkeypatch):
    # The ordering rule lives in the SQL (the mock never executes it),
    # so assert on what the handler sent: open jobs only, posted date
    # known, deadline not passed; highest fit first (safe-cast, since
    # fit_score is TEXT), earliest derived deadline breaks ties.
    cur = _mock_db_recording(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get("/api/lockscreen/current")
    assert resp.status_code == 200
    sql = cur.execute.call_args[0][0]
    assert "apply_status = 'IDLE'" in sql
    assert "posted_at IS NOT NULL" in sql
    assert "posted_at + 7 >= CURRENT_DATE" in sql
    assert "fit_score::numeric" in sql
    assert "DESC NULLS LAST" in sql
    assert "posted_at ASC" in sql
    assert "LIMIT 1" in sql


def test_api_lockscreen_current_no_job_404(monkeypatch):
    _mock_db(monkeypatch, None)
    client = TestClient(app)
    resp = client.get("/api/lockscreen/current")
    assert resp.status_code == 404


def test_api_lockscreen_current_disabled_404(monkeypatch):
    monkeypatch.setattr(settings, "LOCKSCREEN_ENABLED", False)
    _mock_db(monkeypatch, _job_row())
    client = TestClient(app)
    resp = client.get("/api/lockscreen/current")
    assert resp.status_code == 404


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
