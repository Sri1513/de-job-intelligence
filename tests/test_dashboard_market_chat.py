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

# The autouse fixture below replaces market_chat._post_json with a fake;
# keep a reference to the real implementation for the User-Agent test.
_REAL_POST_JSON = market_chat._post_json

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
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": 1, "result": {"resources": [
                {"uri": "jobs://metrics/overview", "name": "Market overview"},
                {"uri": "jobs://metrics/top-skills", "name": "Top skills in demand"},
            ]}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": [
                {"name": "query_jobs_by_criteria", "description": "Search postings."},
            ]}}
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


def test_post_json_sends_browser_user_agent(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(market_chat.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(market_chat, "_post_json", _REAL_POST_JSON)
    assert market_chat._post_json("http://x.test/", {"a": 1}, 5) == {"ok": True}
    assert captured["ua"] and "Mozilla" in captured["ua"]


def _parse_sse(body: str):
    events = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if chunk.startswith("data:"):
            events.append(json.loads(chunk[5:].strip()))
    return events


def test_stream_template_answer():
    client = TestClient(app)
    res = client.post("/api/market-chat/stream", json={"message": "How is python demand?"})
    assert res.status_code == 200
    events = _parse_sse(res.text)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Acme" in text
    assert any(e["type"] == "trace" for e in events)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["llm_used"] is False


def test_stream_llm_tokens(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat/completions")

    def _tokens(messages):
        yield "Hello "
        yield "world"

    monkeypatch.setattr(market_chat, "_stream_llm_tokens", _tokens)
    client = TestClient(app)
    res = client.post("/api/market-chat/stream", json={"message": "hi"})
    assert res.status_code == 200
    events = _parse_sse(res.text)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text == "Hello world"
    done = [e for e in events if e["type"] == "done"][0]
    assert done["llm_used"] is True
    assert "llm_error" not in done


def test_stream_llm_partial_marks_error(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat/completions")

    def _partial(messages):
        yield "Hello "
        raise urllib.error.URLError("cut mid-stream")

    monkeypatch.setattr(market_chat, "_stream_llm_tokens", _partial)
    client = TestClient(app)
    res = client.post("/api/market-chat/stream", json={"message": "hi"})
    events = _parse_sse(res.text)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text == "Hello "
    done = [e for e in events if e["type"] == "done"][0]
    assert done["llm_used"] is True
    assert done["llm_error"] == "unavailable"


def test_stream_agent_picks_tool(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat/completions")
    calls = {"n": 0}

    def _agent_post(url, payload, timeout, headers=None):
        if "public/mcp" in url:
            return _fake_post_json(url, payload, timeout, headers)
        calls["n"] += 1
        if calls["n"] == 1:
            return {"choices": [{"message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": "tools_call",
                    "arguments": json.dumps({
                        "name": "query_jobs_by_criteria",
                        "arguments": {"skill": "python"},
                    }),
                }}],
            }}]}
        return {"choices": [{"message": {"role": "assistant", "content": "final"}}]}

    def _final(messages):
        yield "Done."

    monkeypatch.setattr(market_chat, "_post_json", _agent_post)
    monkeypatch.setattr(market_chat, "_stream_llm_tokens", _final)
    client = TestClient(app)
    res = client.post("/api/market-chat/stream", json={"message": "python jobs?"})
    assert res.status_code == 200
    events = _parse_sse(res.text)
    tool_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["tool"] == "tools_call"
    assert tool_events[0]["arguments"]["name"] == "query_jobs_by_criteria"
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text == "Done."
    trace = [e for e in events if e["type"] == "trace"][0]["trace"]
    assert any(t["method"] == "tools/call" for t in trace)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["llm_used"] is True
    assert "llm_error" not in done


def test_agent_no_tool_calls_falls_back_to_grounded(monkeypatch):
    """Live failure (2026-11-02): the model returned no tool calls,
    and the drawer streamed an empty-context guess (invented skill
    counts). Zero calls must now answer from pre-fetched data."""

    def _agent_post(url, payload, timeout, headers=None):
        if "public/mcp" in url:
            return _fake_post_json(url, payload, timeout, headers)
        return {"choices": [{"message": {"role": "assistant", "content": "no tools"}}]}

    monkeypatch.setattr(market_chat, "_post_json", _agent_post)
    client = TestClient(app)
    res = client.post("/api/market-chat/stream", json={"message": "Which skills are most in demand?"})
    assert res.status_code == 200
    events = _parse_sse(res.text)
    assert not [e for e in events if e["type"] == "tool_call"]
    trace = [e for e in events if e["type"] == "trace"][0]["trace"]
    assert any(t["method"] == "resources/read" for t in trace)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "python" in text.lower()
    assert "1234" not in text


def test_agent_rejects_unknown_resource_and_tool():
    surface, _ = market_chat._discover_mcp_surface()
    trace = []
    assert "Unknown resource" in market_chat._execute_mcp_action(
        "resources_read", {"uri": "jobs://nope"}, trace, surface
    )
    assert "Unknown tool" in market_chat._execute_mcp_action(
        "tools_call", {"name": "drop_table", "arguments": {}}, trace, surface
    )
    assert trace == []


def test_guardrails_file_loads():
    assert market_chat._GUARDRAILS["version"] == 1
    assert market_chat._GUARDRAILS["tools"]["max_tool_rounds"] == 4
    assert market_chat.llm_profile()["model"] == "qwen2.5:3b"
    assert "key" not in json.dumps(market_chat.llm_profile()).lower()


def test_stream_tools_rejected_falls_back_to_grounded(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat/completions")

    def _rejecting(url, payload, timeout, headers=None):
        if "public/mcp" in url:
            return _fake_post_json(url, payload, timeout, headers)
        raise urllib.error.HTTPError(url, 400, "tools not supported", {}, None)

    def _final(messages):
        yield "Grounded."

    monkeypatch.setattr(market_chat, "_post_json", _rejecting)
    monkeypatch.setattr(market_chat, "_stream_llm_tokens", _final)
    client = TestClient(app)
    res = client.post("/api/market-chat/stream", json={"message": "hi"})
    events = _parse_sse(res.text)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text == "Grounded."
    done = [e for e in events if e["type"] == "done"][0]
    assert done["llm_used"] is True


def test_stream_llm_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat/completions")

    def _failing(messages):
        raise urllib.error.URLError("down")
        yield from ()

    monkeypatch.setattr(market_chat, "_stream_llm_tokens", _failing)
    client = TestClient(app)
    res = client.post("/api/market-chat/stream", json={"message": "hi"})
    events = _parse_sse(res.text)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "tracked postings" in text
    done = [e for e in events if e["type"] == "done"][0]
    assert done["llm_used"] is False
    assert done["llm_error"] == "unavailable"


def test_stream_refusal():
    client = TestClient(app)
    res = client.post("/api/market-chat/stream", json={"message": "Write me a python script"})
    events = _parse_sse(res.text)
    done = [e for e in events if e["type"] == "done"][0]
    assert done.get("refused") is True


def test_http_route_and_rate_limit():
    client = TestClient(app)
    res = client.post("/api/market-chat", json={"message": "How is python demand?"})
    assert res.status_code == 200
    assert "Acme" in res.json()["answer"]
    for _ in range(9):
        assert client.post("/api/market-chat", json={"message": "hi"}).status_code == 200
    assert client.post("/api/market-chat", json={"message": "hi"}).status_code == 429
