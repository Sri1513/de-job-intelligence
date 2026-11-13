"""Tests for Sprint 2, Batch 1: Pydantic v2 per-tool argument validation.

`validate_tool_arguments` runs inside `dispatch_tool_call` before any
handler (or background pipeline run) sees the arguments; the HTTP layer
answers a validation failure with JSON-RPC -32602 and field-level errors
only. Handlers are untouched and still receive a plain dict.
"""
import json

import pytest
from starlette.testclient import TestClient

import src.protocols.dispatcher as dispatcher
from src.protocols.app import app
from src.protocols.dispatcher import dispatch_tool_call
from src.protocols.validation import ToolArgumentError, validate_tool_arguments

# Must match tests/conftest.py
HEADERS = {"Authorization": "Bearer test-token"}

client = TestClient(app)


def _job_item(**overrides):
    item = {
        "title": "Senior Data Engineer",
        "company": "Acme",
        "job_url": "https://example.com/j1",
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# validate_tool_arguments: happy paths and defaults
# ---------------------------------------------------------------------------


def test_search_defaults_applied():
    out = validate_tool_arguments("search_saved_jobs", {})
    assert out == {
        "category": "data_engineering",
        "min_score": 0,
        "status": "ALL",
        "limit": 10,
    }


def test_ingestion_defaults_follow_handler_not_old_schema_text():
    out = validate_tool_arguments("run_batch_ingestion", {})
    assert out["search_term"] == "Data Engineer"
    assert out["location"] == "United States"
    assert out["results_wanted"] == 15
    assert out["hours_old"] == 48
    assert out["c2c_only"] is False


def test_numeric_string_coerces_lax():
    out = validate_tool_arguments("search_saved_jobs", {"limit": "50", "min_score": "80"})
    assert out["limit"] == 50
    assert out["min_score"] == 80


def test_bool_string_coerces():
    out = validate_tool_arguments(
        "process_job_alert_draft",
        {"whatsapp_text": "t", "helper_name": "H", "generate_assets": "true"},
    )
    assert out["generate_assets"] is True


# ---------------------------------------------------------------------------
# validate_tool_arguments: rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args",
    [
        ("prepare_job_tailoring_prompt", {}),
        ("get_job_details", {}),
        ("queue_job_for_application", {}),
        ("process_job_alert_draft", {"whatsapp_text": "t"}),
        ("process_job_alert_draft", {"helper_name": "H"}),
        ("stage_agent_jobs", {}),
        ("export_tailored_resume", {}),
    ],
)
def test_missing_required_field(tool, args):
    with pytest.raises(ToolArgumentError) as excinfo:
        validate_tool_arguments(tool, args)
    assert excinfo.value.tool_name == tool
    assert excinfo.value.errors
    assert all("field" in e and "message" in e for e in excinfo.value.errors)


def test_unknown_field_rejected():
    with pytest.raises(ToolArgumentError) as excinfo:
        validate_tool_arguments("search_saved_jobs", {"bogus": 1})
    assert any(e["field"] == "bogus" for e in excinfo.value.errors)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("search_saved_jobs", {"limit": 0}),
        ("search_saved_jobs", {"limit": 101}),
        ("search_saved_jobs", {"min_score": 101}),
        ("search_saved_jobs", {"min_score": -1}),
        ("search_saved_jobs", {"limit": "ten"}),
        ("retry_job_evaluations", {"limit": 101}),
        ("run_batch_ingestion", {"results_wanted": 0}),
        ("run_batch_ingestion", {"hours_old": 721}),
        ("run_application_worker", {"limit": 21}),
        ("run_email_pipeline", {"limit": 0}),
    ],
)
def test_out_of_range_or_wrong_type(tool, args):
    with pytest.raises(ToolArgumentError):
        validate_tool_arguments(tool, args)


def test_stage_jobs_capped_at_100():
    with pytest.raises(ToolArgumentError):
        validate_tool_arguments("stage_agent_jobs", {"jobs": [_job_item() for _ in range(101)]})


def test_stage_jobs_empty_rejected():
    with pytest.raises(ToolArgumentError):
        validate_tool_arguments("stage_agent_jobs", {"jobs": []})


def test_stage_job_item_missing_required():
    with pytest.raises(ToolArgumentError) as excinfo:
        validate_tool_arguments("stage_agent_jobs", {"jobs": [_job_item(title=None)]})
    # The error names the nested field (jobs.0.title).
    assert any("jobs.0.title" in e["field"] for e in excinfo.value.errors)


def test_non_dict_arguments_rejected():
    with pytest.raises(ToolArgumentError) as excinfo:
        validate_tool_arguments("search_saved_jobs", ["not", "a", "dict"])
    assert excinfo.value.errors[0]["field"] == "arguments"


def test_unknown_tool_passes_through():
    args = {"anything": True}
    assert validate_tool_arguments("not_a_tool", args) == args


# ---------------------------------------------------------------------------
# Legacy compat: JSON-string forms and handler-read extras
# ---------------------------------------------------------------------------


def test_stage_job_item_extra_detail_fields_preserved():
    # The tool's contract preserves arbitrary detail fields (workplace,
    # duration, rate text, poster/recruiter, apply method) into metadata,
    # so the item model allows extras and carries them through.
    out = validate_tool_arguments(
        "stage_agent_jobs",
        {
            "jobs": [
                _job_item(workplace="Remote", duration="Not stated", rate_text="$50/hr")
            ]
        },
    )
    assert out["jobs"][0]["workplace"] == "Remote"
    assert out["jobs"][0]["duration"] == "Not stated"
    assert out["jobs"][0]["rate_text"] == "$50/hr"


def test_stage_jobs_json_string_coerced():
    out = validate_tool_arguments(
        "stage_agent_jobs", {"jobs": json.dumps([_job_item()])}
    )
    assert out["jobs"][0]["title"] == "Senior Data Engineer"


def test_stage_jobs_bad_json_string_rejected():
    with pytest.raises(ToolArgumentError):
        validate_tool_arguments("stage_agent_jobs", {"jobs": "{not json"})


def test_export_llm_payload_json_string_coerced():
    out = validate_tool_arguments(
        "export_tailored_resume",
        {"llm_payload": json.dumps({"professional_summary": "x"})},
    )
    assert out["llm_payload"] == {"professional_summary": "x"}


def test_export_tailored_content_alias_accepted():
    out = validate_tool_arguments(
        "export_tailored_resume", {"tailored_content": {"professional_summary": "x"}}
    )
    assert out["tailored_content"] == {"professional_summary": "x"}


def test_export_handler_read_extras_accepted():
    out = validate_tool_arguments(
        "export_tailored_resume",
        {
            "llm_payload": {"professional_summary": "x"},
            "company_name": "Acme",
            "job_title": "Senior Data Engineer",
            "slot_headers": ["a", "b"],
        },
    )
    assert out["company_name"] == "Acme"
    assert out["slot_headers"] == ["a", "b"]


def test_retry_job_ids_and_category_accepted():
    out = validate_tool_arguments(
        "retry_job_evaluations",
        {"limit": 5, "job_ids": ["li-1", "li-2"], "category": "devops"},
    )
    assert out["job_ids"] == ["li-1", "li-2"]
    assert out["category"] == "devops"


def test_worker_max_jobs_alias_accepted():
    out = validate_tool_arguments("run_application_worker", {"max_jobs": 2})
    assert out["max_jobs"] == 2
    assert out["limit"] == 3


def test_aliases_share_models():
    out = validate_tool_arguments("fetch_job_for_tailoring", {"job_id": "li-1"})
    assert out == {"job_id": "li-1"}
    out = validate_tool_arguments(
        "generate_tailored_resume", {"llm_payload": {"professional_summary": "x"}}
    )
    assert out["llm_payload"] == {"professional_summary": "x"}


# ---------------------------------------------------------------------------
# Dispatch: validation runs before any handler (and before the DB)
# ---------------------------------------------------------------------------


async def test_dispatch_validates_before_handler(monkeypatch):
    def _db_touched(*args, **kwargs):
        raise AssertionError("handler/DB must not be reached for invalid args")

    monkeypatch.setattr(dispatcher, "get_db_connection", _db_touched)
    with pytest.raises(ToolArgumentError):
        await dispatch_tool_call("search_saved_jobs", {"limit": 9999})


async def test_dispatch_valid_args_reach_handler(monkeypatch):
    monkeypatch.setattr(
        dispatcher, "get_job_by_id", lambda job_id: {"job_id": job_id, "title": "T"}
    )
    result = await dispatch_tool_call("get_job_details", {"job_id": "li-1"})
    assert result["job_id"] == "li-1"


# ---------------------------------------------------------------------------
# HTTP: tools/call answers -32602 for bad args, 200 for good ones
# ---------------------------------------------------------------------------


def _rpc(tool, arguments, tool_id=1):
    return client.post(
        "/rpc",
        json={
            "jsonrpc": "2.0",
            "id": tool_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=HEADERS,
    )


def test_rpc_bad_args_returns_32602():
    r = _rpc("get_job_details", {})
    assert r.status_code == 200
    err = r.json()["error"]
    assert err["code"] == -32602
    assert err["message"] == "Invalid tool arguments"
    assert err["data"]["tool"] == "get_job_details"
    assert any(e["field"] == "job_id" for e in err["data"]["errors"])
    # No exception text, no traceback in a validation error.
    assert "Traceback" not in json.dumps(err)
    assert "ValueError" not in json.dumps(err)


def test_rpc_out_of_range_returns_32602():
    r = _rpc("search_saved_jobs", {"limit": 9999})
    assert r.json()["error"]["code"] == -32602


def test_rpc_non_dict_arguments_returns_32602():
    r = _rpc("search_saved_jobs", ["not", "a", "dict"])
    assert r.json()["error"]["code"] == -32602


def test_rpc_unknown_tool_unchanged(monkeypatch):
    r = _rpc("not_a_tool", {})
    assert r.status_code == 200
    body = r.json()
    assert "result" in body
    assert "Unknown tool" in body["result"]["content"][0]["text"]


def test_rpc_valid_args_execute(monkeypatch):
    monkeypatch.setattr(
        dispatcher, "get_job_by_id", lambda job_id: {"job_id": job_id, "title": "T"}
    )
    r = _rpc("get_job_details", {"job_id": "li-1"})
    assert r.status_code == 200
    assert "error" not in r.json()
    content = json.loads(r.json()["result"]["content"][0]["text"])
    assert content["job_id"] == "li-1"
