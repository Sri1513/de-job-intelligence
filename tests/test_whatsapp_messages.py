"""WhatsApp inbound messages live in scout.whatsapp_messages, never saved_jobs."""
import json
from unittest.mock import MagicMock, patch

from src.engine.alert_pipeline import (
    DEFAULT_MASTER_RESUME_URL,
    _persist_whatsapp_message,
    process_whatsapp_job_alert,
)


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


_RICH_JD_META = {
    "has_jd": True,
    "company_name": "Nityo",
    "job_title": "Data Engineer",
    "recipient_email": "rec@nityo.example",
    "recruiter_name": "Priya",
    "extracted_jd": "Python, SQL, PySpark, Airflow",
    "short_id": "nity",
}
_SLOTS = {"job1": "herc_rentals", "job2": "a", "job3": "b", "job4": "c"}


def _alert_patches(**overrides):
    """Shared mocks for process_whatsapp_job_alert; returns a patch context."""
    patches = {
        "get_db_connection": patch(
            "src.engine.alert_pipeline.get_db_connection",
            return_value=_mock_conn()[0],
        ),
        "get_or_create_helper": patch(
            "src.engine.alert_pipeline.get_or_create_helper", return_value=7
        ),
        "parse_meta": patch(
            "src.engine.alert_pipeline.parse_whatsapp_alert_metadata",
            return_value=_RICH_JD_META,
        ),
        "select_slots": patch(
            "src.engine.alert_pipeline.select_slot_frameworks",
            return_value=_SLOTS,
        ),
        "load_fw": patch(
            "src.engine.alert_pipeline.load_framework", return_value={}
        ),
        "log_event": patch(
            "src.engine.alert_pipeline.log_outreach_event", return_value=42
        ),
    }
    patches.update(overrides)
    return patches


def test_assets_off_by_default_skips_tailoring_and_draft():
    """Default (flag off): a rich JD still gets no LLM calls, no Drive doc,
    no Gmail draft — the alert is only parsed, logged, and tracked."""
    patches = _alert_patches(
        build_prompt=patch("src.engine.alert_pipeline.build_job_tailoring_prompt"),
        gen_text=patch("src.engine.alert_pipeline.generate_text"),
        export_resume=patch(
            "src.engine.alert_pipeline.export_tailored_resume_to_drive"
        ),
        create_draft=patch("src.engine.alert_pipeline.create_gmail_draft"),
    )
    entered = {name: p.__enter__() for name, p in patches.items()}
    try:
        result = process_whatsapp_job_alert(
            "Data Engineer role at Nityo — Python, SQL, PySpark", "Ganesh"
        )
    finally:
        for p in patches.values():
            p.__exit__(None, None, None)

    entered["build_prompt"].assert_not_called()
    entered["gen_text"].assert_not_called()
    entered["export_resume"].assert_not_called()
    entered["create_draft"].assert_not_called()
    assert result["assets_generated"] is False
    assert result["gmail_draft_id"] is None
    assert result["resume_url"] == DEFAULT_MASTER_RESUME_URL
    assert entered["log_event"].call_args.kwargs["gmail_draft_id"] == ""
    assert entered["log_event"].call_args.kwargs["metadata"]["generate_assets"] is False


def test_assets_on_generates_tailored_resume_and_draft():
    """generate_assets=True preserves the old behavior: tailoring, Drive
    export, outreach email, and Gmail draft all run."""
    patches = _alert_patches(
        build_prompt=patch(
            "src.engine.alert_pipeline.build_job_tailoring_prompt",
            return_value={},
        ),
        gen_tailored=patch(
            "src.engine.alert_pipeline._generate_tailored_content",
            return_value={
                "experience_bullets": {
                    "job1": ["Built pipelines"],
                    "job2": [],
                    "job3": [],
                    "job4": [],
                }
            },
        ),
        export_resume=patch(
            "src.engine.alert_pipeline.export_tailored_resume_to_drive",
            return_value={
                "status": "success",
                "document_url": "https://docs.google.com/tailored",
            },
        ),
        build_email=patch(
            "src.engine.alert_pipeline.build_whatsapp_outreach_prompt",
            return_value="prompt",
        ),
        gen_text=patch(
            "src.engine.alert_pipeline.generate_text",
            return_value=json.dumps({"subject": "S", "body": "B"}),
        ),
        create_draft=patch(
            "src.engine.alert_pipeline.create_gmail_draft",
            return_value={"draft_id": "d-1"},
        ),
    )
    entered = {name: p.__enter__() for name, p in patches.items()}
    try:
        result = process_whatsapp_job_alert(
            "Data Engineer role at Nityo — Python, SQL, PySpark",
            "Ganesh",
            generate_assets=True,
        )
    finally:
        for p in patches.values():
            p.__exit__(None, None, None)

    entered["build_prompt"].assert_called_once()
    entered["export_resume"].assert_called_once()
    entered["gen_text"].assert_called_once()
    entered["create_draft"].assert_called_once()
    assert result["assets_generated"] is True
    assert result["gmail_draft_id"] == "d-1"
    assert result["resume_url"] == "https://docs.google.com/tailored"
