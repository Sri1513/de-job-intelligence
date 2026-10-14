# tests/test_description_heal.py
"""Tests for email-alert description healing.

Regression: email jobs were stored with the one-line placeholder
"Extracted via email alert for ..." and stage_raw_jobs never replaced it —
later full descriptions were discarded as duplicates, and the dashboard
never displayed the description at all.
"""
from unittest.mock import MagicMock, patch

from src.engine.jit_enrichment import _find_qualified_match
from src.engine.pipeline_utils import (
    _is_placeholder_description,
    _is_real_description,
    stage_raw_jobs,
)

REAL_DESC = (
    "We are hiring a Senior Data Engineer to build and operate our Databricks "
    "Lakehouse platform. You will design scalable ETL pipelines in PySpark, "
    "model data in Delta Lake, and partner with analytics teams on reporting. "
    "Requirements: 5+ years of data engineering, strong SQL and Python, "
    "experience with Airflow, dbt, and cloud data warehouses."
)
PLACEHOLDER = "Extracted via email alert for Senior Data Engineer at Colas USA"


def _mock_conn(existing):
    cur = MagicMock()
    cur.fetchone.return_value = existing
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _incoming_job(description):
    return {
        "title": "Senior Data Engineer",
        "company": "Colas USA",
        "job_url": "https://www.linkedin.com/jobs/view/1234567890",
        "location": "United States",
        "description": description,
    }


def test_placeholder_detection():
    assert _is_placeholder_description(PLACEHOLDER) is True
    assert _is_placeholder_description("Role: X at Y. Extracted via scraper.") is True
    assert _is_placeholder_description("") is True
    assert _is_placeholder_description(None) is True
    assert _is_placeholder_description(REAL_DESC) is False
    assert _is_real_description(REAL_DESC) is True
    assert _is_real_description(PLACEHOLDER) is False
    assert _is_real_description("short") is False


@patch("src.engine.pipeline_utils.get_cached_resume", return_value="data engineer resume")
def test_placeholder_healed_by_real_description(_resume):
    conn, cur = _mock_conn(
        {
            "description": PLACEHOLDER,
            "metadata": {
                "locations": [
                    {"city": "United States", "url": "https://www.linkedin.com/jobs/view/1234567890"}
                ]
            },
        }
    )
    stats = stage_raw_jobs(conn, [_incoming_job(REAL_DESC)])
    assert stats["updated"] == 1
    assert stats["skipped"] == 0
    update_calls = [
        c for c in cur.execute.call_args_list if "UPDATE saved_jobs" in str(c.args[0])
    ]
    assert len(update_calls) == 1
    sql, params = update_calls[0].args
    assert "SET description" in sql
    assert params[0] == REAL_DESC


@patch("src.engine.pipeline_utils.get_cached_resume", return_value="data engineer resume")
def test_real_description_not_downgraded_by_placeholder(_resume):
    conn, cur = _mock_conn(
        {
            "description": REAL_DESC,
            "metadata": {
                "locations": [
                    {"city": "United States", "url": "https://www.linkedin.com/jobs/view/1234567890"}
                ]
            },
        }
    )
    stats = stage_raw_jobs(conn, [_incoming_job(PLACEHOLDER)])
    assert stats["skipped"] == 1
    assert stats["updated"] == 0
    update_calls = [
        c for c in cur.execute.call_args_list if "UPDATE saved_jobs" in str(c.args[0])
    ]
    assert update_calls == []


@patch("src.engine.pipeline_utils.get_cached_resume", return_value="data engineer resume")
def test_real_description_not_overwritten_by_other_real(_resume):
    other_real = REAL_DESC.replace("Databricks", "Snowflake")
    conn, cur = _mock_conn(
        {
            "description": REAL_DESC,
            "metadata": {
                "locations": [
                    {"city": "United States", "url": "https://www.linkedin.com/jobs/view/1234567890"}
                ]
            },
        }
    )
    stats = stage_raw_jobs(conn, [_incoming_job(other_real)])
    assert stats["skipped"] == 1
    update_calls = [
        c for c in cur.execute.call_args_list if "UPDATE saved_jobs" in str(c.args[0])
    ]
    assert update_calls == []


def test_exact_linkedin_id_match_beats_company_variant():
    # JobSpy returned the alert's job, but under a company-name variant
    # ("Colas" vs "Colas USA") that fuzzy matching would miss.
    found = [
        {
            "title": "Senior Data Engineer",
            "company": "Colas",
            "location": "Remote",
            "job_url": "https://www.linkedin.com/jobs/view/1234567890/?tracking=abc",
            "description": REAL_DESC,
        }
    ]
    match = _find_qualified_match(found, "Colas USA", linkedin_id="1234567890")
    assert match is found[0]


def test_fuzzy_match_still_works_without_id():
    found = [
        {
            "title": "Senior Data Engineer",
            "company": "Colas USA",
            "location": "United States",
            "job_url": "https://www.linkedin.com/jobs/view/555/?tracking=x",
            "description": REAL_DESC,
        }
    ]
    match = _find_qualified_match(found, "Colas USA", linkedin_id=None)
    assert match is found[0]


def test_wrong_id_does_not_match():
    found = [
        {
            "title": "Senior Data Engineer",
            "company": "Some Other Co",
            "location": "United States",
            "job_url": "https://www.linkedin.com/jobs/view/999/?tracking=x",
            "description": REAL_DESC,
        }
    ]
    assert _find_qualified_match(found, "Colas USA", linkedin_id="1234567890") is None


@patch("src.workers.email_pipeline.get_db_connection")
@patch("src.workers.email_pipeline.run_backfill_batch", return_value={"successful": 0})
@patch("src.workers.email_pipeline.stage_raw_jobs", return_value={"saved": 1, "updated": 0, "skipped": 0})
@patch("src.engine.jit_enrichment.fetch_scraped_jobs")
@patch("src.workers.email_pipeline.fetch_jobs_from_email")
def test_widening_falls_back_to_indeed(
    mock_fetch_email, mock_scraped, mock_stage, _backfill, mock_conn
):
    from src.workers.email_pipeline import run_email_pipeline

    mock_fetch_email.return_value = [
        {
            "title": "Senior Data Engineer, Databricks",
            "company": "Colas USA",
            "location": "United States",
            "job_url": "https://www.linkedin.com/jobs/view/999",
            "description": PLACEHOLDER,
            "employment_type": "Unknown",
            "sponsorship": "Not Mentioned",
            "source": "Email Alert",
            "date_posted": "2026-10-14",
        }
    ]
    indeed_job = {
        "title": "Senior Data Engineer",
        "company": "Colas USA",
        "location": "United States",
        "job_url": "https://www.indeed.com/viewjob?jk=abc",
        "description": REAL_DESC,
        "is_remote": True,
    }
    # LinkedIn widening returns nothing (429-blocked); Indeed finds the job.
    mock_scraped.side_effect = [[], [indeed_job]]
    mock_conn.return_value.__enter__.return_value = MagicMock()

    result = run_email_pipeline(limit=1)

    assert result["status"] == "success"
    assert mock_scraped.call_count == 2
    assert mock_scraped.call_args_list[0].kwargs["site_name"] == ["linkedin"]
    assert mock_scraped.call_args_list[1].kwargs["site_name"] == ["indeed"]
    staged = mock_stage.call_args.kwargs["jobs"]
    assert staged[0]["description"] == REAL_DESC
    assert staged[0]["job_url"] == "https://www.indeed.com/viewjob?jk=abc"


_EMAIL_ITEM = {
    "title": "Senior Data Engineer, Databricks",
    "company": "Colas USA",
    "location": "United States",
    "job_url": "https://www.linkedin.com/jobs/view/999",
    "description": PLACEHOLDER,
    "employment_type": "Unknown",
    "sponsorship": "Not Mentioned",
    "source": "Email Alert",
    "date_posted": "2026-10-14",
}


@patch("src.workers.email_pipeline.get_db_connection")
@patch("src.workers.email_pipeline.run_backfill_batch", return_value={"successful": 0})
@patch("src.workers.email_pipeline.stage_raw_jobs", return_value={"saved": 1, "updated": 0, "skipped": 0})
@patch("src.engine.jit_enrichment.fetch_linkedin_guest_job", return_value=None)
@patch("src.engine.jit_enrichment.fetch_scraped_jobs")
@patch("src.workers.email_pipeline.fetch_jobs_from_email")
def test_widening_falls_back_to_zip_recruiter(
    mock_fetch_email, mock_scraped, mock_guest, mock_stage, _backfill, mock_conn
):
    from src.workers.email_pipeline import run_email_pipeline

    mock_fetch_email.return_value = [dict(_EMAIL_ITEM)]
    zip_job = {
        "title": "Senior Data Engineer",
        "company": "Colas USA",
        "location": "United States",
        "job_url": "https://www.ziprecruiter.com/jobs/abc",
        "description": REAL_DESC,
        "is_remote": True,
    }
    # LinkedIn + Indeed miss; ZipRecruiter finds the job.
    mock_scraped.side_effect = [[], [], [zip_job]]
    mock_conn.return_value.__enter__.return_value = MagicMock()

    result = run_email_pipeline(limit=1)

    assert result["status"] == "success"
    assert mock_scraped.call_count == 3
    assert mock_scraped.call_args_list[2].kwargs["site_name"] == ["zip_recruiter"]
    assert mock_guest.call_count == 0  # board hit — guest API never reached
    staged = mock_stage.call_args.kwargs["jobs"]
    assert staged[0]["description"] == REAL_DESC
    assert staged[0]["job_url"] == "https://www.ziprecruiter.com/jobs/abc"


@patch("src.workers.email_pipeline.get_db_connection")
@patch("src.workers.email_pipeline.run_backfill_batch", return_value={"successful": 0})
@patch("src.workers.email_pipeline.stage_raw_jobs", return_value={"saved": 1, "updated": 0, "skipped": 0})
@patch("src.engine.jit_enrichment.fetch_linkedin_guest_job")
@patch("src.engine.jit_enrichment.fetch_scraped_jobs")
@patch("src.workers.email_pipeline.fetch_jobs_from_email")
def test_widening_falls_back_to_guest_api(
    mock_fetch_email, mock_scraped, mock_guest, mock_stage, _backfill, mock_conn
):
    from src.workers.email_pipeline import run_email_pipeline

    mock_fetch_email.return_value = [dict(_EMAIL_ITEM)]
    mock_scraped.side_effect = [[], [], []]  # all three boards miss
    mock_guest.return_value = {
        "title": "Senior Data Engineer",
        "company": "Colas USA",
        "location": "United States",
        "description": REAL_DESC,
        "job_url": "https://www.linkedin.com/jobs/view/999/",
        "job_type": "N/A",
        "date_posted": "N/A",
        "site": "linkedin",
    }
    mock_conn.return_value.__enter__.return_value = MagicMock()

    result = run_email_pipeline(limit=1)

    assert result["status"] == "success"
    assert mock_scraped.call_count == 3
    mock_guest.assert_called_once_with("999")  # ID from the alert's job URL
    staged = mock_stage.call_args.kwargs["jobs"]
    assert staged[0]["description"] == REAL_DESC
    assert staged[0]["job_url"] == "https://www.linkedin.com/jobs/view/999/"
