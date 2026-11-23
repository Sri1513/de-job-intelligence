# tests/test_deadline_queue.py
"""Tests for the separate deadline queue + advance notification.

A job enters the queue once its derived deadline (posted_at + 7 days)
has passed and it is still IDLE. The daily check re-verifies each
queued job; only VERIFIED outcomes are recorded — an unreachable check
never marks a job closed (fail-open). A notification fires only when
the job that WAS the current dream job was determined closed/rejected
this run AND the current job actually changed.
"""
import pytest

from src.workers.deadline_queue_check import (
    check_posting_live,
    check_queued_jobs,
    determine_outcome,
    draft_advance_message,
    enqueue_expired_jobs,
    run_daily_check,
)

LIVE_PAGE = (
    "<html><body><h1>Senior Data Engineer</h1><p>Join our team to build "
    "data pipelines with Python, SQL, and Airflow in the cloud.</p></body></html>"
)
CLOSED_PAGE = (
    "<html><body><h1>Senior Data Engineer</h1><p>This job is no longer "
    "available. Browse similar roles.</p></body></html>"
)


class FakeCursor:
    """Statements recorded; fetchone/fetchall dispatch on SQL text."""

    def __init__(self, current_rows=None, enqueue_rows=None, tracking_rows=None):
        # fetchone pops: the daily run snapshots the current job twice
        # (before, after), so the list is consumed in order.
        self._current = list(current_rows or [])
        self.enqueue_rows = list(enqueue_rows or [])
        self.tracking_rows = list(tracking_rows or [])
        self.statements = []
        self._last_sql = ""

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        if "posted_at + 7 >= CURRENT_DATE" in self._last_sql:
            return self._current.pop(0) if self._current else None
        return None

    def fetchall(self):
        if "INSERT INTO scout.deadline_queue" in self._last_sql:
            return self.enqueue_rows
        if "FROM scout.deadline_queue dq" in self._last_sql:
            return self.tracking_rows
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def _fetcher(mapping):
    def fetch(url):
        result = mapping[url]
        if isinstance(result, Exception):
            raise result
        return result

    return fetch


def _tracking_row(job_id, url, apply_status="IDLE", metadata=None):
    return {
        "job_id": job_id,
        "deadline": "2026-11-28",
        "title": f"Role {job_id}",
        "company": "Acme",
        "job_url": url,
        "apply_status": apply_status,
        "metadata": metadata,
    }


def _current_row(job_id):
    return {
        "job_id": job_id,
        "title": f"Role {job_id}",
        "company": "Acme",
        "location": "USA",
        "fit_score": "70",
        "posted_at": "2026-11-21",
    }


class TestEnqueueRule:
    def test_returns_enqueued_ids(self):
        cur = FakeCursor(enqueue_rows=[{"job_id": "li-1"}, {"job_id": "li-2"}])
        assert enqueue_expired_jobs(FakeConnection(cur)) == ["li-1", "li-2"]

    def test_sql_guards_expired_idle_unqueued_only(self):
        cur = FakeCursor(enqueue_rows=[])
        enqueue_expired_jobs(FakeConnection(cur))
        sql = cur.statements[0][0]
        assert "apply_status = 'IDLE'" in sql
        assert "posted_at + 7 < CURRENT_DATE" in sql
        assert "posted_at IS NOT NULL" in sql
        assert "NOT EXISTS" in sql
        assert "deadline_queue" in sql
        assert "ON CONFLICT (job_id) DO NOTHING" in sql


class TestCheckPostingLive:
    def test_404_is_closed(self):
        assert check_posting_live("https://x/1", _fetcher({"https://x/1": (404, "")})) == "closed"

    def test_410_is_closed(self):
        assert check_posting_live("https://x/1", _fetcher({"https://x/1": (410, "")})) == "closed"

    def test_closed_marker_is_closed(self):
        assert (
            check_posting_live("https://x/1", _fetcher({"https://x/1": (200, CLOSED_PAGE)}))
            == "closed"
        )

    def test_clean_page_is_live(self):
        assert (
            check_posting_live("https://x/1", _fetcher({"https://x/1": (200, LIVE_PAGE)}))
            == "live"
        )

    def test_network_error_is_unknown(self):
        assert (
            check_posting_live(
                "https://x/1", _fetcher({"https://x/1": ConnectionError("down")})
            )
            == "unknown"
        )

    @pytest.mark.parametrize("status", [403, 429, 500, 502])
    def test_blocked_or_server_error_is_unknown(self, status):
        assert (
            check_posting_live("https://x/1", _fetcher({"https://x/1": (status, "")}))
            == "unknown"
        )

    def test_empty_url_is_unknown(self):
        assert check_posting_live(None) == "unknown"
        assert check_posting_live("") == "unknown"

    def test_empty_body_is_unknown(self):
        assert (
            check_posting_live("https://x/1", _fetcher({"https://x/1": (200, "<html></html>")}))
            == "unknown"
        )


class TestDetermineOutcome:
    def test_tracker_rejected_dict(self):
        row = {"apply_status": "IDLE", "metadata": {"tracker": {"stage": "rejected"}}}
        assert determine_outcome(row, "live") == "rejected"

    def test_tracker_rejected_json_string(self):
        row = {
            "apply_status": "IDLE",
            "metadata": '{"tracker": {"outcome": "rejected"}}',
        }
        assert determine_outcome(row, "live") == "rejected"

    def test_rejected_beats_applied(self):
        row = {
            "apply_status": "SUBMITTED",
            "metadata": {"tracker": {"stage": "rejected"}},
        }
        assert determine_outcome(row, "closed") == "rejected"

    @pytest.mark.parametrize("status", ["SUBMITTED", "MANUAL"])
    def test_applied_status(self, status):
        row = {"apply_status": status, "metadata": {}}
        assert determine_outcome(row, "live") == "applied"

    def test_closed(self):
        assert determine_outcome({"apply_status": "IDLE", "metadata": {}}, "closed") == "closed"

    def test_still_open(self):
        assert (
            determine_outcome({"apply_status": "IDLE", "metadata": {}}, "live") == "still_open"
        )

    def test_unknown_yields_none(self):
        assert determine_outcome({"apply_status": "IDLE", "metadata": {}}, "unknown") is None


class TestCheckQueuedJobs:
    def test_closed_row_resolves_done(self):
        cur = FakeCursor(tracking_rows=[_tracking_row("li-1", "https://x/1")])
        determinations = check_queued_jobs(
            FakeConnection(cur), fetcher=_fetcher({"https://x/1": (404, "")})
        )
        assert determinations == [
            {"job_id": "li-1", "title": "Role li-1", "company": "Acme", "outcome": "closed"}
        ]
        updates = [s for s in cur.statements if "SET state =" in s[0]]
        assert len(updates) == 1
        assert updates[0][1] == ("done", "closed", "li-1")

    def test_unknown_row_only_touches_last_checked(self):
        cur = FakeCursor(tracking_rows=[_tracking_row("li-1", "https://x/1")])
        determinations = check_queued_jobs(
            FakeConnection(cur),
            fetcher=_fetcher({"https://x/1": ConnectionError("down")}),
        )
        assert determinations == []
        assert not any("SET state =" in s[0] for s in cur.statements)
        touches = [s for s in cur.statements if "SET last_checked_at = NOW()" in s[0]]
        assert len(touches) == 1
        assert touches[0][1] == ("li-1",)

    def test_still_open_row_stays_tracking(self):
        cur = FakeCursor(tracking_rows=[_tracking_row("li-1", "https://x/1")])
        determinations = check_queued_jobs(
            FakeConnection(cur), fetcher=_fetcher({"https://x/1": (200, LIVE_PAGE)})
        )
        assert determinations[0]["outcome"] == "still_open"
        updates = [s for s in cur.statements if "SET state =" in s[0]]
        assert updates[0][1] == ("tracking", "still_open", "li-1")


def _run(before, after, tracking_rows, mapping):
    cur = FakeCursor(
        current_rows=[b for b in (before, after)],
        tracking_rows=tracking_rows,
    )
    conn = FakeConnection(cur)
    sent = []

    def draft(old, reason, new):
        return f"DRAFT {old['job_id']} | {reason} | {new['job_id'] if new else 'NONE'}"

    def sender(msg):
        sent.append(msg)
        return True

    summary = run_daily_check(
        conn, fetcher=_fetcher(mapping), draft_fn=draft, sender=sender
    )
    return summary, sent, conn


class TestRunDailyCheck:
    def test_notifies_when_current_closed_and_pointer_moves(self):
        summary, sent, conn = _run(
            _current_row("li-1"),
            _current_row("li-2"),
            [_tracking_row("li-1", "https://x/1")],
            {"https://x/1": (404, "")},
        )
        assert summary["notified"] is True
        assert summary["previous_current"] == "li-1"
        assert summary["current"] == "li-2"
        assert len(sent) == 1
        assert "li-1" in sent[0] and "li-2" in sent[0]
        assert conn.commits == 1

    def test_no_notify_when_current_unchanged(self):
        summary, sent, _ = _run(
            _current_row("li-1"),
            _current_row("li-1"),
            [_tracking_row("li-9", "https://x/9")],
            {"https://x/9": (404, "")},
        )
        assert summary["notified"] is False
        assert sent == []

    def test_no_notify_when_departed_job_only_expired(self):
        summary, sent, _ = _run(
            _current_row("li-1"),
            _current_row("li-2"),
            [_tracking_row("li-1", "https://x/1")],
            {"https://x/1": (200, LIVE_PAGE)},
        )
        # Pointer moved by deadline expiry alone (still live) — no
        # verified closed/rejected determination, so no notification.
        assert summary["notified"] is False
        assert sent == []

    def test_no_notify_when_no_current_before(self):
        summary, sent, _ = _run(
            None,
            _current_row("li-2"),
            [_tracking_row("li-1", "https://x/1")],
            {"https://x/1": (404, "")},
        )
        assert summary["notified"] is False
        assert sent == []

    def test_unreachable_checks_change_nothing(self):
        summary, sent, conn = _run(
            _current_row("li-1"),
            _current_row("li-1"),
            [_tracking_row("li-1", "https://x/1")],
            {"https://x/1": ConnectionError("down")},
        )
        assert summary["determinations"] == []
        assert summary["notified"] is False
        assert sent == []
        # Only the last_checked_at touch — no row resolved to 'done'.
        assert not any("SET state =" in s[0] for s in conn._cursor.statements)

    def test_notifies_with_no_next_job(self):
        summary, sent, _ = _run(
            _current_row("li-1"),
            None,
            [_tracking_row("li-1", "https://x/1")],
            {"https://x/1": (404, "")},
        )
        assert summary["notified"] is True
        assert "NONE" in sent[0]


class TestDraftAdvanceMessage:
    def test_llm_failure_uses_template(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("quota")

        monkeypatch.setattr(
            "src.workers.deadline_queue_check.generate_text", _boom
        )
        msg = draft_advance_message(
            {"title": "Data Engineer", "company": "Acme"},
            "the posting closed",
            {"title": "Data Engineer", "company": "Beta"},
        )
        assert "Data Engineer at Acme" in msg
        assert "the posting closed" in msg
        assert "Data Engineer at Beta" in msg

    def test_llm_draft_used_when_available(self, monkeypatch):
        monkeypatch.setattr(
            "src.workers.deadline_queue_check.generate_text",
            lambda *args, **kwargs: "  canned draft  ",
        )
        msg = draft_advance_message(
            {"title": "Data Engineer", "company": "Acme"},
            "the posting closed",
            {"title": "Data Engineer", "company": "Beta"},
        )
        assert msg == "canned draft"
