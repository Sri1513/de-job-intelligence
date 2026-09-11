# tests/test_ingestion.py
from unittest.mock import patch, MagicMock
from src.ingestion.dice_client import DiceJobClient
from src.workers.batch_ingestion import persist_jobs

@patch("src.ingestion.dice_client.requests.Session.get")
def test_dice_client_search_jobs(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "12345",
                "title": "Lead Data Engineer",
                "companyName": "Acme Corp",
                "location": "Remote, US",
                "summary": "Must know Python, Spark, and AWS."
            }
        ]
    }
    mock_get.return_value = mock_response

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
            "description": "Python, PySpark, Snowflake"
        }
    ]

    count = persist_jobs(mock_jobs, category_slug="data_engineering")
    assert count == 1
    assert mock_cur.execute.called
    assert mock_conn.commit.called