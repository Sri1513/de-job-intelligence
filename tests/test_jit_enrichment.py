# tests/test_jit_enrichment.py
"""Tests for just-in-time description enrichment on the tailoring path.

Contract: when build_job_tailoring_prompt loads a job whose description is
still an email-alert placeholder, one live widening pass replaces it and
heals the DB row. Fail-open everywhere: any miss or error leaves the job
unchanged. A failed attempt is attempt-guarded so the next tailoring run
doesn't re-scrape.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.engine.jit_enrichment import maybe_enrich_job_for_tailoring

REAL_DESC = (
    "We are hiring a Senior Data Engineer to build and operate our Databricks "
    "Lakehouse platform. You will design scalable ETL pipelines in PySpark, "
    "model data in Delta Lake with medallion architecture, and partner with "
    "analytics teams on reporting. Requirements: 5+ years of data engineering, "
    "strong SQL and Python, experience with Airflow, dbt, and cloud warehouses."
)
PLACEHOLDER = "Extracted via email alert for Senior Data Engineer at Colas USA"

ENRICHED_JOB = {
    "title": "Senior Data Engineer",
    "company": "Colas USA",
    "location": "United States",
    "job_url": "https://www.linkedin.com/jobs/view/999/",
    "description": REAL_DESC,
}


def _job(**overrides):
    job = {
        "job_id": "li-999",
        "title": "Senior Data Engineer, Databricks",
        "company": "Colas USA",
        "location": "United States",
        "job_url": "https://www.linkedin.com/jobs/view/999",
        "description": PLACEHOLDER,
        "job_category": "data_engineering",
        "metadata": {},
    }
    job.update(overrides)
    return job


def _mock_conn():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    return conn, cur


@patch("src.engine.jit_enrichment.calculate_local_fit_score")
@patch("src.engine.jit_enrichment.get_cached_resume", return_value="resume text")
@patch("src.engine.jit_enrichment.enrich_job_description")
def test_jit_enriches_placeholder_and_writes_back(
    mock_enrich, _resume, mock_fit
):
    import json

    mock_enrich.return_value = dict(ENRICHED_JOB)
    mock_fit.return_value = {
        "score": 82,
        "matched_skills": ["python"],
        "missing_skills": [],
        "semantic_match": 80,
        "skill_match": 85,
    }
    conn, cur = _mock_conn()

    job = maybe_enrich_job_for_tailoring(_job(), conn)

    assert job["description"] == REAL_DESC
    assert job["job_url"] == "https://www.linkedin.com/jobs/view/999/"
    mock_enrich.assert_called_once()
    desc_updates = [
        c for c in cur.execute.call_args_list if "SET description" in c.args[0]
    ]
    assert len(desc_updates) == 1
    params = desc_updates[0].args[1]
    assert params[0] == REAL_DESC
    assert params[1] == "82"
    assert params[3] == "li-999"
    meta = json.loads(params[2])
    assert meta["jit_enrichment"]["attempts"] == 1
    assert meta["matched_skills"] == ["python"]


@patch("src.engine.jit_enrichment.enrich_job_description")
def test_jit_skips_real_description(mock_enrich):
    conn, cur = _mock_conn()
    job = maybe_enrich_job_for_tailoring(_job(description=REAL_DESC), conn)
    assert job["description"] == REAL_DESC
    mock_enrich.assert_not_called()
    cur.execute.assert_not_called()


@patch("src.engine.jit_enrichment.enrich_job_description")
def test_jit_skips_recent_failed_attempt(mock_enrich):
    conn, cur = _mock_conn()
    meta = {
        "jit_enrichment": {
            "last_attempt": datetime.now(timezone.utc).isoformat(),
            "attempts": 2,
        }
    }
    job = maybe_enrich_job_for_tailoring(_job(metadata=meta), conn)
    assert job["description"] == PLACEHOLDER
    mock_enrich.assert_not_called()
    cur.execute.assert_not_called()


@patch("src.engine.jit_enrichment.enrich_job_description", return_value=None)
def test_jit_retries_after_guard_expiry(mock_enrich):
    conn, cur = _mock_conn()
    meta = {
        "jit_enrichment": {
            "last_attempt": (
                datetime.now(timezone.utc) - timedelta(days=8)
            ).isoformat(),
            "attempts": 1,
        }
    }
    maybe_enrich_job_for_tailoring(_job(metadata=meta), conn)
    mock_enrich.assert_called_once()


@patch("src.engine.jit_enrichment.enrich_job_description", return_value=None)
def test_jit_records_failed_attempt_only(_mock_enrich):
    import json

    conn, cur = _mock_conn()
    job = maybe_enrich_job_for_tailoring(_job(), conn)

    assert job["description"] == PLACEHOLDER  # unchanged
    meta_updates = [
        c for c in cur.execute.call_args_list if "SET metadata" in c.args[0]
    ]
    desc_updates = [
        c for c in cur.execute.call_args_list if "SET description" in c.args[0]
    ]
    assert len(meta_updates) == 1
    assert len(desc_updates) == 0  # guard only, no description write
    saved_meta = json.loads(meta_updates[0].args[1][0])
    assert saved_meta["jit_enrichment"]["attempts"] == 1
    assert "last_attempt" in saved_meta["jit_enrichment"]


@patch(
    "src.engine.jit_enrichment.enrich_job_description",
    side_effect=RuntimeError("boom"),
)
def test_jit_fail_open_on_exception(_mock_enrich):
    conn, cur = _mock_conn()
    job = _job()
    result = maybe_enrich_job_for_tailoring(job, conn)  # must not raise
    assert result["description"] == PLACEHOLDER


@patch("src.engine.jit_enrichment.enrich_job_description")
def test_jit_override_path_enriches_in_memory(mock_enrich):
    mock_enrich.return_value = dict(ENRICHED_JOB)
    override = _job()
    override.pop("job_id")  # WhatsApp overrides may carry no DB row
    job = maybe_enrich_job_for_tailoring(override, conn=None)
    assert job["description"] == REAL_DESC
    assert job["metadata"]["jit_enrichment"]["attempts"] == 1


# --- wiring: build_job_tailoring_prompt triggers the JIT hook ---


def _patch_prompt_builder_common(mock_enrich):
    """Patch the heavy downstream builders; enrich swaps in the real desc."""
    def _fake_enrich(job, conn=None):
        job = dict(job)
        job["description"] = REAL_DESC
        return job

    mock_enrich.side_effect = _fake_enrich
    patches = [
        patch(
            "src.synthesis.prompt_builder.select_slot_frameworks",
            return_value={
                "job1": "herc_rentals",
                "job2": "blue_yonder",
                "job3": "accenture",
                "job4": "thomson_reuters",
            },
        ),
        patch(
            "src.synthesis.prompt_builder.load_framework",
            return_value={
                "resume_slot": {},
                "summary_angle": "",
                "domain": "",
                "bullet_bank": [],
                "process_framework": {},
            },
        ),
        patch(
            "src.synthesis.prompt_builder.load_role_config",
            return_value={
                "role_title": "Data Engineer",
                "boundaries": "",
                "bridging_rules": "",
                "skills_schema": {},
            },
        ),
        patch(
            "src.synthesis.prompt_builder.load_target_resume",
            return_value="resume text",
        ),
        patch(
            "src.synthesis.prompt_builder.load_skill_equivalences",
            return_value={"equivalences": {}},
        ),
        patch(
            "src.engine.bullet_ranker.rank_all_slots",
            return_value={"job1": [], "job2": [], "job3": [], "job4": []},
        ),
    ]
    for p in patches:
        p.start()
    return patches


@patch("src.synthesis.prompt_builder.maybe_enrich_job_for_tailoring")
def test_tailoring_prompt_enriches_override(mock_enrich):
    from src.synthesis.prompt_builder import build_job_tailoring_prompt

    patches = _patch_prompt_builder_common(mock_enrich)
    try:
        override = _job()
        bundle = build_job_tailoring_prompt("li-999", job_override=override)
    finally:
        for p in patches:
            p.stop()

    mock_enrich.assert_called_once()
    args, kwargs = mock_enrich.call_args
    assert args[0]["description"] == PLACEHOLDER
    assert kwargs.get("conn") is None  # override path: no DB write-back
    assert bundle["job_description"] == REAL_DESC


@patch("src.synthesis.prompt_builder.maybe_enrich_job_for_tailoring")
@patch("src.synthesis.prompt_builder.get_db_connection")
def test_tailoring_prompt_enriches_db_job(mock_conn_fn, mock_enrich):
    from src.synthesis.prompt_builder import build_job_tailoring_prompt

    patches = _patch_prompt_builder_common(mock_enrich)
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = _job()
    mock_conn_fn.return_value.__enter__.return_value = conn
    try:
        bundle = build_job_tailoring_prompt("li-999")
    finally:
        for p in patches:
            p.stop()

    mock_enrich.assert_called_once()
    args, kwargs = mock_enrich.call_args
    assert args[0]["description"] == PLACEHOLDER
    assert args[1] is conn  # DB path: live connection for write-back
    assert bundle["job_description"] == REAL_DESC
