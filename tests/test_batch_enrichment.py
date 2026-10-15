# tests/test_batch_enrichment.py
"""Tests for guest-API enrichment of LinkedIn descriptions in batch ingestion.

Regression: since ~2026-10-02 LinkedIn 429/signup-walls JobSpy's per-job
/jobs/view/ page fetch from datacenter IPs, so every LinkedIn batch job
arrived with an empty description and stage_raw_jobs stamped the
"Role: ..." placeholder (Indeed descriptions flowed fine throughout, so
this was LinkedIn-specific, not a staging bug). The guest endpoint still
answers; this fills the description before staging.
"""
from unittest.mock import MagicMock, patch

from src.engine.ingestion import _enrich_missing_linkedin_descriptions

REAL_DESC = (
    "We are hiring a Sr. Data Engineer to build and operate our Databricks "
    "Lakehouse platform. You will design scalable ETL pipelines in PySpark, "
    "model data in Delta Lake with medallion architecture, and partner with "
    "analytics teams on reporting. Requirements: 5+ years of data engineering, "
    "strong SQL and Python, experience with Airflow and cloud warehouses."
)
LINKEDIN_URL = "https://www.linkedin.com/jobs/view/4466288002"


def _job(**overrides):
    job = {
        "job_id": "li-4466288002",
        "title": "Sr. Data Engineer",
        "company": "Carrot",
        "location": "United States",
        "job_url": LINKEDIN_URL,
        "description": "N/A",  # what JobSpy yields after fillna on an empty desc
    }
    job.update(overrides)
    return job


def _guest_job(desc=REAL_DESC):
    return {
        "title": "Sr. Data Engineer",
        "company": "Carrot",
        "location": "United States",
        "job_url": LINKEDIN_URL + "/",
        "description": desc,
    }


@patch("time.sleep")
@patch("src.engine.ingestion.fetch_linkedin_guest_job")
def test_enriches_missing_linkedin_description(mock_guest, _sleep):
    mock_guest.return_value = _guest_job()
    job = _job()
    assert _enrich_missing_linkedin_descriptions([job]) == 1
    assert job["description"] == REAL_DESC
    mock_guest.assert_called_once_with("4466288002")


@patch("time.sleep")
@patch("src.engine.ingestion.fetch_linkedin_guest_job")
def test_skips_jobs_with_real_descriptions(mock_guest, _sleep):
    job = _job(description=REAL_DESC)
    assert _enrich_missing_linkedin_descriptions([job]) == 0
    mock_guest.assert_not_called()
    assert job["description"] == REAL_DESC


@patch("time.sleep")
@patch("src.engine.ingestion.fetch_linkedin_guest_job")
def test_skips_non_linkedin_jobs(mock_guest, _sleep):
    job = _job(job_id="indeed-abc", job_url="https://www.indeed.com/viewjob?jk=abc")
    assert _enrich_missing_linkedin_descriptions([job]) == 0
    mock_guest.assert_not_called()


@patch("time.sleep")
@patch("src.engine.ingestion.fetch_linkedin_guest_job", return_value=None)
def test_stops_after_consecutive_misses(_mock_guest, _sleep):
    jobs = [_job(job_id=f"li-100{i}", job_url=f"https://www.linkedin.com/jobs/view/100{i}")
            for i in range(5)]
    assert _enrich_missing_linkedin_descriptions(jobs) == 0
    # Breaker: 3 consecutive misses stop the run; jobs 4-5 never attempted.
    assert _mock_guest.call_count == 3
    assert all(j["description"] == "N/A" for j in jobs)


@patch("time.sleep")
@patch("src.engine.ingestion.fetch_linkedin_guest_job", side_effect=RuntimeError("boom"))
def test_fail_open_on_exception(_mock_guest, _sleep):
    job = _job()
    assert _enrich_missing_linkedin_descriptions([job]) == 0  # must not raise
    assert job["description"] == "N/A"


@patch("time.sleep")
@patch("src.engine.ingestion.fetch_linkedin_guest_job")
def test_rejects_short_guest_description(mock_guest, _sleep):
    mock_guest.return_value = _guest_job(desc="Short blurb, not a real posting.")
    job = _job()
    assert _enrich_missing_linkedin_descriptions([job]) == 0
    assert job["description"] == "N/A"  # too short to trust; placeholder path kept


@patch("time.sleep")
@patch("src.engine.ingestion.fetch_linkedin_guest_job")
def test_success_resets_miss_counter(mock_guest, _sleep):
    mock_guest.side_effect = [None, None, _guest_job(), None, None, None]
    jobs = [_job(job_id=f"li-200{i}", job_url=f"https://www.linkedin.com/jobs/view/200{i}")
            for i in range(6)]
    assert _enrich_missing_linkedin_descriptions(jobs) == 1
    assert jobs[2]["description"] == REAL_DESC
    # 2 misses, 1 hit (counter reset), then 3 misses -> breaker trips: 6 calls.
    assert mock_guest.call_count == 6


@patch("time.sleep")
@patch("src.engine.ingestion.fetch_linkedin_guest_job")
def test_batch_workflow_enriches_before_staging(mock_guest, _sleep):
    from src.engine import ingestion as ing

    mock_guest.return_value = _guest_job()
    with (
        patch.object(ing, "fetch_scraped_jobs", return_value=[_job()]),
        patch.object(ing, "DiceJobClient") as mock_dice_cls,
        patch.object(ing, "stage_raw_jobs",
                     return_value={"saved": 1, "updated": 0, "skipped": 0}) as mock_stage,
        patch.object(ing, "get_db_connection") as mock_conn,
        patch.object(ing, "run_backfill_batch", return_value={"successful": 0}),
    ):
        mock_dice_cls.return_value.search_jobs.return_value = []
        mock_conn.return_value.__enter__.return_value = MagicMock()

        result = ing.run_batch_ingestion_workflow()

    assert result["status"] == "success"
    staged = mock_stage.call_args.kwargs["jobs"]
    assert staged[0]["description"] == REAL_DESC  # enriched, not placeholder-bound
