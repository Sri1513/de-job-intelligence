# tests/test_posted_date.py
"""Tests for posted-date capture (7-day deadline system, foundation).

The posted date is the REAL date a job was posted, per source — never the
ingest/saved date (owner decision 2026-11-22). A source with no usable
posted date stores NULL. No backfill of old rows.
"""
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

from src.engine.pipeline_utils import stage_raw_jobs
from src.ingestion.dice_client import normalize_dice_job
from src.ingestion.email_scraper import parse_linkedin_plain_text_email
from src.ingestion.posted_date import extract_posted_date_from_text, parse_posted_date

REF = date(2026, 11, 23)


class TestParsePostedDate:
    def test_iso_date_string(self):
        assert parse_posted_date("2026-11-21", REF) == date(2026, 11, 21)

    def test_iso_datetime_string(self):
        assert parse_posted_date("2026-11-21T10:30:00", REF) == date(2026, 11, 21)
        assert parse_posted_date("2026-11-21T10:30:00+00:00", REF) == date(2026, 11, 21)

    def test_datetime_and_date_objects(self):
        assert parse_posted_date(datetime(2026, 11, 20, 15, 0), REF) == date(2026, 11, 20)
        assert parse_posted_date(date(2026, 11, 20), REF) == date(2026, 11, 20)

    def test_relative_days(self):
        assert parse_posted_date("2 days ago", REF) == date(2026, 11, 21)
        assert parse_posted_date("1 day ago", REF) == date(2026, 11, 22)
        assert parse_posted_date("Posted 2 days ago", REF) == date(2026, 11, 21)
        assert parse_posted_date("2d ago", REF) == date(2026, 11, 21)

    def test_relative_today_yesterday_hours(self):
        assert parse_posted_date("today", REF) == REF
        assert parse_posted_date("just now", REF) == REF
        assert parse_posted_date("yesterday", REF) == date(2026, 11, 22)
        assert parse_posted_date("5 hours ago", REF) == REF

    def test_relative_weeks_months(self):
        assert parse_posted_date("3 weeks ago", REF) == date(2026, 11, 2)
        assert parse_posted_date("1 month ago", REF) == date(2026, 10, 24)

    def test_sentinels_yield_none(self):
        for sentinel in (None, "", "N/A", "Unknown", "Recently", "none"):
            assert parse_posted_date(sentinel, REF) is None

    def test_garbage_yields_none(self):
        assert parse_posted_date("garbage text", REF) is None
        assert parse_posted_date("2026-13-45", REF) is None
        assert parse_posted_date(12345, REF) is None

    def test_slash_date(self):
        assert parse_posted_date("2026/11/21", REF) == date(2026, 11, 21)


class TestExtractFromText:
    def test_finds_relative_mention(self):
        assert extract_posted_date_from_text("Posted 2 days ago", REF) == date(
            2026, 11, 21
        )

    def test_plain_lines_yield_none(self):
        assert extract_posted_date_from_text("New York, NY", REF) is None
        assert extract_posted_date_from_text("", REF) is None


class TestDicePostedDate:
    def test_iso_posted_date_kept(self):
        job = normalize_dice_job(
            {"id": "1", "title": "Data Engineer", "postedDate": "2026-11-20T08:00:00Z"}
        )
        assert job["date_posted"] == "2026-11-20"

    def test_missing_posted_date_is_none_not_recently(self):
        job = normalize_dice_job({"id": "1", "title": "Data Engineer"})
        assert job["date_posted"] is None


class TestEmailAlertPostedDate:
    def _body(self, *extra_lines):
        lines = ["Senior Data Engineer", "Acme Corp", *extra_lines]
        return "\n".join(lines) + "\n"

    def test_relative_date_line_parsed(self):
        body = self._body(
            "New York, NY",
            "Posted 2 days ago",
            "View job: https://www.linkedin.com/jobs/view/1",
        )
        jobs = parse_linkedin_plain_text_email(body, REF)
        assert len(jobs) == 1
        assert jobs[0]["date_posted"] == "2026-11-21"
        assert jobs[0]["location"] == "New York, NY"

    def test_date_line_before_location_is_not_the_location(self):
        body = self._body(
            "Posted 3 days ago",
            "Austin, TX",
            "View job: https://www.linkedin.com/jobs/view/1",
        )
        jobs = parse_linkedin_plain_text_email(body, REF)
        assert len(jobs) == 1
        assert jobs[0]["date_posted"] == "2026-11-20"
        assert jobs[0]["location"] == "Austin, TX"

    def test_no_date_line_is_none_not_today(self):
        body = self._body(
            "New York, NY",
            "View job: https://www.linkedin.com/jobs/view/1",
        )
        jobs = parse_linkedin_plain_text_email(body, REF)
        assert len(jobs) == 1
        assert jobs[0]["date_posted"] is None


def _mock_conn(existing):
    cur = MagicMock()
    cur.fetchone.return_value = existing
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _incoming_job(**overrides):
    job = {
        "title": "Data Engineer",
        "company": "Acme Corp",
        "job_url": "https://www.linkedin.com/jobs/view/9999999999",
        "location": "United States",
        "description": "Short desc",
    }
    job.update(overrides)
    return job


def _insert_params(cur):
    calls = [c for c in cur.execute.call_args_list if "INSERT INTO saved_jobs" in str(c.args[0])]
    assert len(calls) == 1
    return calls[0].args[1]


class TestStageRawJobsPostedAt:
    @patch("src.engine.pipeline_utils.get_cached_resume", return_value="resume")
    def test_insert_stores_real_posted_date(self, _resume):
        conn, cur = _mock_conn(None)
        stats = stage_raw_jobs(conn, [_incoming_job(date_posted="2026-11-21")])
        assert stats["saved"] == 1
        assert _insert_params(cur)[-1] == date(2026, 11, 21)

    @patch("src.engine.pipeline_utils.get_cached_resume", return_value="resume")
    def test_insert_stores_null_when_source_has_none(self, _resume):
        conn, cur = _mock_conn(None)
        stats = stage_raw_jobs(conn, [_incoming_job(date_posted=None)])
        assert stats["saved"] == 1
        assert _insert_params(cur)[-1] is None

    @patch("src.engine.pipeline_utils.get_cached_resume", return_value="resume")
    def test_insert_normalizes_relative_date(self, _resume):
        conn, cur = _mock_conn(None)
        stats = stage_raw_jobs(conn, [_incoming_job(date_posted="2 days ago")])
        assert stats["saved"] == 1
        # Relative to today: "2 days ago" resolves against today by
        # construction (the parser's default reference).
        assert _insert_params(cur)[-1] == date.today() - timedelta(days=2)

    @patch("src.engine.pipeline_utils.get_cached_resume", return_value="resume")
    def test_restage_heals_null_posted_at(self, _resume):
        url = "https://www.linkedin.com/jobs/view/9999999999"
        existing = {
            "description": "A real description that is long enough to not be a placeholder. "
            "It describes a data engineering role with Python, SQL, Airflow, and cloud "
            "experience across several paragraphs of genuine posting text.",
            "metadata": {"locations": [{"city": "United States", "url": url}]},
            "posted_at": None,
        }
        conn, cur = _mock_conn(existing)
        stats = stage_raw_jobs(conn, [_incoming_job(date_posted="2026-11-21")])
        assert stats["updated"] == 1
        updates = [c for c in cur.execute.call_args_list if "UPDATE saved_jobs" in str(c.args[0])]
        assert len(updates) == 1
        sql, params = updates[0].args
        assert "posted_at" in sql
        assert date(2026, 11, 21) in params

    @patch("src.engine.pipeline_utils.get_cached_resume", return_value="resume")
    def test_restage_never_overwrites_stored_posted_at(self, _resume):
        url = "https://www.linkedin.com/jobs/view/9999999999"
        existing = {
            "description": "A real description that is long enough to not be a placeholder. "
            "It describes a data engineering role with Python, SQL, Airflow, and cloud "
            "experience across several paragraphs of genuine posting text.",
            "metadata": {"locations": [{"city": "United States", "url": url}]},
            "posted_at": date(2026, 11, 20),
        }
        conn, cur = _mock_conn(existing)
        stats = stage_raw_jobs(conn, [_incoming_job(date_posted="2026-11-21")])
        assert stats["skipped"] == 1
        assert stats["updated"] == 0
        updates = [c for c in cur.execute.call_args_list if "UPDATE saved_jobs" in str(c.args[0])]
        assert updates == []
