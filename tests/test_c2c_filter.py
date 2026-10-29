# tests/test_c2c_filter.py
"""C2C-only ingestion: arrangement detection + workflow threading."""

from unittest.mock import MagicMock, patch

from src.engine.ingestion import run_batch_ingestion_workflow
from src.engine.scraper import is_c2c_arrangement


def test_c2c_positive_markers():
    assert is_c2c_arrangement({"title": "Data Engineer - C@C", "description": ""})
    assert is_c2c_arrangement(
        {"title": "Data Engineer", "description": "Contract Corp To Corp, 12 months."}
    )
    assert is_c2c_arrangement(
        {"title": "Data Engineer", "description": "We accept C2C candidates."}
    )
    assert is_c2c_arrangement({"title": "Data Engineer", "description": "C to C only."})
    assert is_c2c_arrangement(
        {"title": "Data Engineer", "description": "W2 or C2C, both fine."}
    )


def test_c2c_exclusions_win_over_mentions():
    assert not is_c2c_arrangement(
        {"title": "Data Engineer", "description": "No C2C only W2."}
    )
    assert not is_c2c_arrangement(
        {"title": "Data Engineer", "description": "No sponsorship or C2C for this role."}
    )
    assert not is_c2c_arrangement(
        {
            "title": "Data Engineer",
            "description": (
                "Only open to applicants who can be engaged on a W-2 basis. "
                "1099 contractors and Corp-to-Corp arrangements will not be considered."
            ),
        }
    )
    assert not is_c2c_arrangement(
        {"title": "Data Engineer", "description": "Contract W2 only."}
    )


def test_c2c_requires_confirmation():
    assert not is_c2c_arrangement({"title": "", "description": ""})
    assert not is_c2c_arrangement({"title": "Data Engineer", "description": "N/A"})
    assert not is_c2c_arrangement(
        {"title": "Data Engineer", "description": "1099 contractor welcome."}
    )
    assert not is_c2c_arrangement(
        {"title": "Data Engineer", "description": "Contract role, 12 months."}
    )


def _mock_workflow_deps(mock_fetch, mock_dice_cls, mock_enrich, mock_stage, mock_eval, mock_db):
    mock_enrich.return_value = 0
    mock_stage.return_value = {"saved": 2, "skipped": 0, "updated": 0}
    mock_eval.return_value = {"successful": 0}
    mock_db.return_value.__enter__.return_value = MagicMock()
    dice_client = MagicMock()
    mock_dice_cls.return_value = dice_client
    return dice_client


@patch("src.engine.ingestion.get_db_connection")
@patch("src.engine.ingestion.run_backfill_batch")
@patch("src.engine.ingestion.stage_raw_jobs")
@patch("src.engine.ingestion._enrich_missing_linkedin_descriptions")
@patch("src.engine.ingestion.DiceJobClient")
@patch("src.engine.ingestion.fetch_scraped_jobs")
def test_workflow_c2c_only_filters_after_enrichment(
    mock_fetch, mock_dice_cls, mock_enrich, mock_stage, mock_eval, mock_db
):
    dice_client = _mock_workflow_deps(
        mock_fetch, mock_dice_cls, mock_enrich, mock_stage, mock_eval, mock_db
    )
    mock_fetch.return_value = [
        {"title": "Data Engineer", "description": "C2C contract, 12 months.", "job_url": "https://example.com/1"},
        {"title": "Data Engineer", "description": "Full-time W2 role.", "job_url": "https://example.com/2"},
    ]
    dice_client.search_jobs.return_value = [
        {"title": "Data Engineer", "description": "Corp to Corp engagement.", "source": "dice", "job_url": "https://example.com/3"},
        {"title": "Data Engineer", "description": "Contract role.", "source": "dice", "job_url": "https://example.com/4"},
    ]

    result = run_batch_ingestion_workflow(c2c_only=True)

    assert mock_fetch.call_args.kwargs["c2c_only"] is True
    staged = mock_stage.call_args.kwargs["jobs"]
    assert [job["job_url"] for job in staged] == [
        "https://example.com/1",
        "https://example.com/3",
    ]
    assert result["c2c_only"] is True
    assert result["c2c_dropped"] == 2


@patch("src.engine.ingestion.get_db_connection")
@patch("src.engine.ingestion.run_backfill_batch")
@patch("src.engine.ingestion.stage_raw_jobs")
@patch("src.engine.ingestion._enrich_missing_linkedin_descriptions")
@patch("src.engine.ingestion.DiceJobClient")
@patch("src.engine.ingestion.fetch_scraped_jobs")
def test_workflow_default_stages_everything(
    mock_fetch, mock_dice_cls, mock_enrich, mock_stage, mock_eval, mock_db
):
    dice_client = _mock_workflow_deps(
        mock_fetch, mock_dice_cls, mock_enrich, mock_stage, mock_eval, mock_db
    )
    mock_fetch.return_value = [
        {"title": "Data Engineer", "description": "C2C contract.", "job_url": "https://example.com/1"},
        {"title": "Data Engineer", "description": "Full-time W2 role.", "job_url": "https://example.com/2"},
    ]
    dice_client.search_jobs.return_value = []

    result = run_batch_ingestion_workflow()

    assert mock_fetch.call_args.kwargs["c2c_only"] is False
    assert len(mock_stage.call_args.kwargs["jobs"]) == 2
    assert result["c2c_only"] is False
    assert result["c2c_dropped"] == 0
