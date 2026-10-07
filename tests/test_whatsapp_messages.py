"""WhatsApp inbound messages live in scout.whatsapp_messages, never saved_jobs."""
from unittest.mock import MagicMock, patch

from src.engine.alert_pipeline import _persist_whatsapp_message


def _mock_conn():
    conn = MagicMock()
    cur = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_persist_whatsapp_message_targets_new_table():
    conn, cur = _mock_conn()
    with patch("src.engine.alert_pipeline.get_db_connection", return_value=conn):
        message_id = _persist_whatsapp_message("Data Engineer role at Ask Consulting", sender="Ganesh")

    assert message_id.startswith("wa-")
    sql = cur.execute.call_args[0][0]
    assert "scout.whatsapp_messages" in sql
    assert "saved_jobs" not in sql
    params = cur.execute.call_args[0][1]
    assert params[0] == message_id
    assert params[1] == "Ganesh"
    assert "Ask Consulting" in params[2]


def test_persist_whatsapp_message_dedupes_on_conflict():
    conn, cur = _mock_conn()
    with patch("src.engine.alert_pipeline.get_db_connection", return_value=conn):
        first = _persist_whatsapp_message("same message twice")
        second = _persist_whatsapp_message("same message twice")
    assert first == second
    assert "ON CONFLICT (message_id) DO NOTHING" in cur.execute.call_args[0][0]
