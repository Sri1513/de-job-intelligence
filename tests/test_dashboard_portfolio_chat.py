# tests/test_dashboard_portfolio_chat.py
"""Tests for the portfolio AI assistant backend (sriomkar.com drawer).

Mirrors tests/test_dashboard_market_chat.py, plus the tests that are
unique to this route:
- surface isolation: the portfolio route can never reach a jobs URI
  or the jobs tool, even when live discovery offers them and even
  when the model asks for them;
- grounded fallback: zero tool calls -> answer from pre-fetched
  profile data, never an empty-context guess;
- CORS: exactly the guardrailed origins, never a wildcard.
All network calls are faked by monkeypatching the module's
_mcp_rpc / _post_json; no database or live MCP server is involved.
"""
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.dashboard import portfolio_chat
from src.dashboard.app import app

PROFILE = json.loads(
    (Path(__file__).resolve().parent.parent / "config" / "company_frameworks.json").read_text(
        encoding="utf-8"
    )
)
PROFILE_TEXT = json.dumps(PROFILE)

# What a FULL (unfiltered) MCP server listing looks like: jobs
# resources, the jobs tool, and the profile resource. The portfolio
# route must filter this down to the profile resource only.
FULL_RESOURCES = [
    {"uri": "jobs://metrics/overview", "name": "Market overview"},
    {"uri": "jobs://metrics/top-skills", "name": "Top skills in demand"},
    {"uri": "profile://frameworks", "name": "Omkar's public profile"},
]
FULL_TOOLS = [{"name": "query_jobs_by_criteria", "description": "Search postings."}]


def _fake_mcp_rpc(method, params):
    """Fake JSON-RPC transport returning (request, response) pairs,
    mirroring market_chat._mcp_rpc's contract."""
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    if method == "resources/list":
        return req, {"jsonrpc": "2.0", "id": 1, "result": {"resources": FULL_RESOURCES}}
    if method == "tools/list":
        return req, {"jsonrpc": "2.0", "id": 1, "result": {"tools": FULL_TOOLS}}
    if method == "resources/read":
        uri = (params or {}).get("uri")
        if uri == "profile://frameworks":
            text = PROFILE_TEXT
        elif uri == "jobs://metrics/overview":
            text = json.dumps([{"job_category": "data_engineering", "total_jobs_tracked": 1748}])
        else:
            text = json.dumps([{"skill": "python", "job_count": 900}])
        return req, {"jsonrpc": "2.0", "id": 1, "result": {"contents": [{"text": text}]}}
    if method == "tools/call":
        return req, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": "[]"}]}}
    raise AssertionError(f"unexpected MCP method {method}")


@pytest.fixture(autouse=True)
def _fake_network(monkeypatch):
    monkeypatch.setattr(portfolio_chat, "_mcp_rpc", _fake_mcp_rpc)
    monkeypatch.setattr(portfolio_chat, "_post_json", _fake_llm_post)
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "")
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_KEY", "")
    portfolio_chat._rate_buckets.clear()


def _fake_llm_post(url, payload, timeout, headers=None):
    if "llm.test" in url:
        return {"choices": [{"message": {"content": "LLM-grounded profile answer."}}]}
    raise AssertionError(f"unexpected url {url}")


def _parse_sse(body: str):
    events = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if chunk.startswith("data:"):
            events.append(json.loads(chunk[5:].strip()))
    return events


def test_happy_path_returns_profile_answer_and_trace():
    result = portfolio_chat.answer_question("What experience does Omkar have?", "1.2.3.4")
    assert "Optum" in result["answer"]
    assert "Akkodis" in result["answer"]
    assert [t["method"] for t in result["trace"]] == ["resources/read"]
    # The only MCP call is the profile read -- never a jobs URI.
    assert result["trace"][0]["request"]["params"]["uri"] == "profile://frameworks"
    assert result["llm_used"] is False


def test_profile_question_passes_scope():
    result = portfolio_chat.answer_question("Tell me about his Databricks work.", "1.2.3.4")
    assert result.get("refused") is not True
    assert "Databricks" in result["answer"]


def test_off_profile_refused_without_mcp_calls():
    calls = []

    def _recording(method, params):
        calls.append(method)
        return _fake_mcp_rpc(method, params)

    portfolio_chat._mcp_rpc = _recording
    for question in (
        "Write me a python script",
        "Does he need visa sponsorship?",
        "What salary does he expect?",
    ):
        result = portfolio_chat.answer_question(question, "1.2.3.4")
        assert result.get("refused") is True, question
        assert result["answer"] == portfolio_chat.REFUSAL_TEXT
    assert calls == []


def test_phone_number_is_shareable():
    # The phone is public (resume, LinkedIn, profile contact) -- a direct
    # ask must be answered from the profile, not refused.
    result = portfolio_chat.answer_question("What is his phone number?", "1.2.3.4")
    assert result.get("refused") is not True
    assert "+1 (951) 545-2146" in result["answer"]


def test_profile_file_is_public_safe():
    blob = json.dumps(PROFILE).lower()
    for forbidden in ("sponsorship", "visa", "salary", "hourly", "h-1b", "green card"):
        assert forbidden not in blob, forbidden
    assert PROFILE["experience"][0]["period"] == "Oct 2025 - Present"
    assert "Python" in PROFILE["skills"]
    assert "dbt" in PROFILE["skills"]


def test_surface_filter_drops_jobs_even_when_discovered():
    surface, trace = portfolio_chat._discover_portfolio_surface()
    assert [r["uri"] for r in surface["resources"]] == ["profile://frameworks"]
    assert surface["tools"] == []
    # The trace still shows the full discovery (auditability), but
    # the surface handed to the model is filtered.
    listed = trace[0]["response"]["result"]["resources"]
    assert len(listed) == 3


def test_dispatch_offers_only_resources_read_for_profile():
    surface, _ = portfolio_chat._discover_portfolio_surface()
    tools = portfolio_chat._portfolio_dispatch_tools(surface)
    assert [t["function"]["name"] for t in tools] == ["resources_read"]
    assert tools[0]["function"]["parameters"]["properties"]["uri"]["enum"] == [
        "profile://frameworks"
    ]


def test_execute_rejects_jobs_uri_and_jobs_tool_without_mcp_call():
    surface, _ = portfolio_chat._discover_portfolio_surface()
    calls = []

    def _recording(method, params):
        calls.append((method, params))
        return _fake_mcp_rpc(method, params)

    portfolio_chat._mcp_rpc = _recording
    trace = []
    assert "Unknown resource" in portfolio_chat._execute_portfolio_action(
        "resources_read", {"uri": "jobs://metrics/overview"}, trace, surface
    )
    assert "Unknown MCP action" in portfolio_chat._execute_portfolio_action(
        "tools_call",
        {"name": "query_jobs_by_criteria", "arguments": {"skill": "python"}},
        trace,
        surface,
    )
    assert "Unknown resource" in portfolio_chat._execute_portfolio_action(
        "resources_read", {"uri": "jobs://metrics/top-skills"}, trace, surface
    )
    assert calls == []
    assert trace == []


def test_agent_model_asking_for_jobs_tool_gets_grounded_fallback(monkeypatch):
    """Isolation under a hostile model: it asks for the jobs tool,
    the backend refuses without an MCP call, and the loop ends on
    the grounded profile answer (zero VALID tool calls)."""
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat/completions")
    mcp_calls = []

    def _recording_mcp(method, params):
        mcp_calls.append((method, params))
        return _fake_mcp_rpc(method, params)

    def _agent_post(url, payload, timeout, headers=None):
        if payload.get("tools") is not None:
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

    monkeypatch.setattr(portfolio_chat, "_mcp_rpc", _recording_mcp)
    monkeypatch.setattr(portfolio_chat, "_post_json", _agent_post)
    client = TestClient(app)
    res = client.post("/api/portfolio-chat/stream", json={"message": "python jobs?"})
    assert res.status_code == 200
    events = _parse_sse(res.text)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Optum" in text
    assert not any(
        method == "tools/call" for method, _ in mcp_calls
    )
    assert not any(
        params.get("uri", "").startswith("jobs://") for _, params in mcp_calls
    )


def test_agent_no_tool_calls_falls_back_to_grounded(monkeypatch):
    """Expected launch path (market-chat d29c650 lesson): the VM2 3B
    model answers without calling any tool, and the drawer must
    answer from pre-fetched profile data, not an empty-context guess."""

    def _agent_post(url, payload, timeout, headers=None):
        return {"choices": [{"message": {"role": "assistant", "content": "no tools"}}]}

    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat/completions")
    monkeypatch.setattr(portfolio_chat, "_post_json", _agent_post)
    client = TestClient(app)
    res = client.post(
        "/api/portfolio-chat/stream",
        json={"message": "What experience does Omkar have?"},
        headers={"Origin": "https://sriomkar.com"},
    )
    assert res.status_code == 200
    events = _parse_sse(res.text)
    assert not [e for e in events if e["type"] == "tool_call"]
    trace = [e for e in events if e["type"] == "trace"][0]["trace"]
    assert any(
        t["method"] == "resources/read"
        and t["request"]["params"]["uri"] == "profile://frameworks"
        for t in trace
    )
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Optum" in text


def test_stream_template_answer_without_llm():
    client = TestClient(app)
    res = client.post(
        "/api/portfolio-chat/stream",
        json={"message": "What experience does Omkar have?"},
        headers={"Origin": "https://www.sriomkar.com"},
    )
    assert res.status_code == 200
    assert res.headers.get("Access-Control-Allow-Origin") == "https://www.sriomkar.com"
    events = _parse_sse(res.text)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Optum" in text
    assert any(e["type"] == "trace" for e in events)
    done = [e for e in events if e["type"] == "done"][0]
    assert done["llm_used"] is False


def test_cors_allows_only_guardrailed_origins():
    client = TestClient(app)
    for origin in ("https://sriomkar.com", "https://www.sriomkar.com"):
        res = client.post(
            "/api/portfolio-chat", json={"message": "hi"}, headers={"Origin": origin}
        )
        assert res.headers.get("Access-Control-Allow-Origin") == origin, origin
    res = client.post(
        "/api/portfolio-chat",
        json={"message": "hi"},
        headers={"Origin": "https://evil.example"},
    )
    assert "Access-Control-Allow-Origin" not in res.headers
    assert "*" not in res.headers.get("Access-Control-Allow-Origin", "")


def test_cors_preflight():
    client = TestClient(app)
    res = client.options(
        "/api/portfolio-chat",
        headers={
            "Origin": "https://sriomkar.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert res.status_code == 204
    assert res.headers.get("Access-Control-Allow-Origin") == "https://sriomkar.com"
    assert "POST" in res.headers.get("Access-Control-Allow-Methods", "")
    res = client.options(
        "/api/portfolio-chat", headers={"Origin": "https://evil.example"}
    )
    assert res.status_code == 204
    assert "Access-Control-Allow-Origin" not in res.headers


def test_http_route_and_rate_limit():
    client = TestClient(app)
    res = client.post("/api/portfolio-chat", json={"message": "What experience does Omkar have?"})
    assert res.status_code == 200
    assert "Optum" in res.json()["answer"]
    for _ in range(9):
        assert client.post("/api/portfolio-chat", json={"message": "hi"}).status_code == 200
    assert client.post("/api/portfolio-chat", json={"message": "hi"}).status_code == 429


def test_guardrails_file_loads():
    assert portfolio_chat._GUARDRAILS["version"] == 3
    assert portfolio_chat._GUARDRAILS["tools"]["max_tool_rounds"] == 4
    assert portfolio_chat._GUARDRAILS["tools"]["allowed_resources"] == ["profile://frameworks"]
    assert portfolio_chat._GUARDRAILS["tools"]["allowed_tools"] == []
    assert portfolio_chat.llm_profile()["model"] == "qwen2.5:3b"
    assert "key" not in json.dumps(portfolio_chat.llm_profile()).lower()
    assert set(portfolio_chat.ALLOWED_ORIGINS) == {
        "https://sriomkar.com",
        "https://www.sriomkar.com",
    }


def test_prompts_require_third_person_about_omkar():
    for prompt in (portfolio_chat.SYSTEM_PROMPT, portfolio_chat.AGENT_SYSTEM_PROMPT):
        assert "third person" in prompt
        assert "Never claim to be Sri Omkar" in prompt
        assert "Portfolio Assistant" in prompt


def test_contact_includes_phone_and_each_link_once():
    contact = PROFILE["contact"]
    assert contact["phone"] == "+1 (951) 545-2146"
    assert contact["github"] == "https://github.com/sri1513"
    assert contact["linkedin"] == "https://linkedin.com/in/sri-omkar-58r4r4r8"
    assert contact["website"] == "https://sriomkar.com"
    values = list(contact.values())
    assert len(values) == len(set(values))
    for prompt in (portfolio_chat.SYSTEM_PROMPT, portfolio_chat.AGENT_SYSTEM_PROMPT):
        assert "never repeat a URL" in prompt
    rendered = portfolio_chat._template_answer("how to contact Omkar?", PROFILE)
    for value in values:
        assert rendered.count(value) == 1


def test_prompts_require_first_person_identity_and_complete_source():
    for prompt in (portfolio_chat.SYSTEM_PROMPT, portfolio_chat.AGENT_SYSTEM_PROMPT):
        assert "I am the Portfolio Assistant" in prompt
        assert "never say you lack access" in prompt
        assert "list ALL contact methods" in prompt


def test_llm_path_used_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat")
    result = portfolio_chat.answer_question("What experience does Omkar have?", "1.2.3.4")
    assert result["answer"] == "LLM-grounded profile answer."
    assert result["llm_used"] is True


def test_mcp_down_returns_unavailable_answer(monkeypatch):
    def _down(method, params):
        raise RuntimeError("down")

    monkeypatch.setattr(portfolio_chat, "_mcp_rpc", _down)
    result = portfolio_chat.answer_question("What experience does Omkar have?", "1.2.3.4")
    assert "temporarily unavailable" in result["answer"]
