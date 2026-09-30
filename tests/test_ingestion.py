# tests/test_ingestion.py
from unittest.mock import AsyncMock, MagicMock, patch

from src.ingestion.dice_client import DiceJobClient
from src.workers.batch_ingestion import persist_jobs


@patch("src.ingestion.dice_client.Client")
def test_dice_client_search_jobs(mock_client_cls):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    mock_result = MagicMock()
    mock_result.structured_content = {
        "data": [
            {
                "id": "12345",
                "title": "Lead Data Engineer",
                "companyName": "Acme Corp",
                "jobLocation": {"displayName": "Remote, US"},
                "isRemote": True,
                "jobDescription": "Must know Python, Spark, and AWS.",
            }
        ]
    }
    mock_client.call_tool = AsyncMock(return_value=mock_result)

    client = DiceJobClient()
    jobs = client.search_jobs("Data Engineer")

    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "dice_12345"
    assert jobs[0]["company"] == "Acme Corp"
    assert jobs[0]["is_remote"] is True


@patch("src.workers.batch_ingestion.get_db_connection")
def test_persist_jobs_batch(mock_get_db):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    mock_jobs = [
        {
            "job_id": "test_1",
            "title": "Data Engineer",
            "company": "Test Co",
            "location": "Remote",
            "is_remote": True,
            "job_url": "https://example.com/job",
            "description": "Python, PySpark, Snowflake",
        }
    ]

    count = persist_jobs(mock_jobs, category_slug="data_engineering")
    assert count == 1
    assert mock_cur.execute.called
    assert mock_conn.commit.called
