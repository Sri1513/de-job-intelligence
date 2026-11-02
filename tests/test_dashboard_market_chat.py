# tests/test_dashboard_market_chat.py
"""Tests for the "Ask Market Intelligence" drawer backend.

The dashboard acts as an MCP client: it POSTs JSON-RPC to the public
MCP surface and returns the exact payloads as a wire trace. All network
calls are faked here by monkeypatching the module's _post_json; no
database or live MCP server is involved.
"""
import json
import urllib.error

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard import market_chat
from src.dashboard.app import app

OVERVIEW = [
    # Real MCP shape: plain psycopg rows serialized as JSON arrays, in
    # the SELECT column order (job_category, total_jobs_tracked,
    # average_fit_score, high_match_jobs, unique_hiring_companies,
    # last_ingested_at). The drawer must normalize these; dict-shaped
    # fakes masked the production 500 this test file now guards.
    ["data_engineering", 1748, 49.6, 12, 300, "2026-11-01T00:00:00"]
]
TOP_SKILLS = [
    ["python", 900],
    ["sql", 800],
]
JOB_ROWS = [
    ["li-1", "Data Engineer", "Acme", "Remote", "data_engineering", 88,
     "2026-11-01T00:00:00", "https://example.com/j", "Preview", 88]
]


def _fake_post_json(url, payload, timeout, headers=None):
    if "public/mcp" in url:
        method = payload.get("method")
        params = payload.get("params") or {}
        if method == "resources/read":
            if params.get("uri") == "jobs://metrics/overview":
                text = json.dumps(OVERVIEW)
            else:
                text = json.dumps(TOP_SKILLS)
            return {"jsonrpc": "2.0", "id": 1, "result": {"contents": [{"text": text}]}}
        if method == "tools/call":
            assert params["arguments"]["skill"] == "python"
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"text": json.dumps(JOB_ROWS)}]},
            }
    if "llm.test" in url:
        return {"choices": [{"message": {"content": "LLM-grounded answer."}}]}
    raise AssertionError(f"unexpected url {url}")


@pytest.fixture(autouse=True)
def _fake_network(monkeypatch):
    monkeypatch.setattr(market_chat, "_post_json", _fake_post_json)
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "")
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_KEY", "")
    market_chat._rate_buckets.clear()


def test_happy_path_returns_answer_and_trace():
    result = market_chat.answer_question("How is python demand?", "1.2.3.4")
    assert "Acme" in result["answer"]
    assert "python" in result["answer"]
    assert [t["method"] for t in result["trace"]] == [
        "resources/read",
        "resources/read",
        "tools/call",
    ]
    assert result["llm_used"] is False


def test_dict_shaped_rows_still_work(monkeypatch):
    def _dict_rows(url, payload, timeout, headers=None):
        if "public/mcp" in url:
            params = payload.get("params") or {}
            if payload.get("method") == "resources/read":
                text = json.dumps(
                    [{"job_category": "data_engineering", "total_jobs_tracked": 5,
                      "average_fit_score": 50, "high_match_jobs": 1,
                      "unique_hiring_companies": 2}]
                    if params.get("uri") == "jobs://metrics/overview"
                    else [{"skill": "python", "job_count": 9}]
                )
                return {"jsonrpc": "2.0", "id": 1, "result": {"contents": [{"text": text}]}}
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"text": json.dumps(
                    [{"title": "Data Engineer", "company": "Acme",
                      "location": "Remote", "fit_score": 88}])}]},
            }
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(market_chat, "_post_json", _dict_rows)
    result = market_chat.answer_question("How is python demand?", "1.2.3.4")
    assert "Acme" in result["answer"]


def test_off_topic_refused_without_mcp_calls():
    calls = []

    def _recording(url, payload, timeout, headers=None):
        calls.append(url)
        return _fake_post_json(url, payload, timeout, headers)

    market_chat._post_json = _recording
    result = market_chat.answer_question("Write me a python script", "1.2.3.4")
    assert result["refused"] is True
    assert calls == []


def test_llm_path_used_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat")
    result = market_chat.answer_question("Which skills are most in demand?", "1.2.3.4")
    assert result["answer"] == "LLM-grounded answer."
    assert result["llm_used"] is True


def test_llm_ollama_shape_also_parsed(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat")

    def _ollama_shape(url, payload, timeout, headers=None):
        if "llm.test" in url:
            return {"message": {"content": "Ollama-native answer."}}
        return _fake_post_json(url, payload, timeout, headers)

    monkeypatch.setattr(market_chat, "_post_json", _ollama_shape)
    result = market_chat.answer_question("Which skills are most in demand?", "1.2.3.4")
    assert result["answer"] == "Ollama-native answer."
    assert result["llm_used"] is True


def test_llm_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat")

    def _failing(url, payload, timeout, headers=None):
        if "llm.test" in url:
            raise urllib.error.URLError("connection refused")
        return _fake_post_json(url, payload, timeout, headers)

    monkeypatch.setattr(market_chat, "_post_json", _failing)
    result = market_chat.answer_question("Which skills are most in demand?", "1.2.3.4")
    assert "tracked postings" in result["answer"]
    assert result["llm_used"] is False
    assert result["llm_error"] == "unavailable"


def test_mcp_down_returns_unavailable_answer(monkeypatch):
    def _down(url, payload, timeout, headers=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(market_chat, "_post_json", _down)
    result = market_chat.answer_question("Which skills are most in demand?", "1.2.3.4")
    assert "temporarily unavailable" in result["answer"]
    assert all("error" in t for t in result["trace"])


def test_http_route_and_rate_limit():
    client = TestClient(app)
    res = client.post("/api/market-chat", json={"message": "How is python demand?"})
    assert res.status_code == 200
    assert "Acme" in res.json()["answer"]
    for _ in range(9):
        assert client.post("/api/market-chat", json={"message": "hi"}).status_code == 200
    assert client.post("/api/market-chat", json={"message": "hi"}).status_code == 429
