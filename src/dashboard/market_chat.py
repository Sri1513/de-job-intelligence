# src/dashboard/market_chat.py
"""
"Ask Market Intelligence" drawer backend for jobs.sriomkar.com.

The dashboard acts as an MCP client: it POSTs JSON-RPC to the public MCP
surface (src/protocols/dashboard_mcp.py, reached inside the compose
network as MARKET_CHAT_MCP_URL) and shows the exact request/response
payloads in a collapsible wire trace, so a visitor can verify the
protocol mechanics instead of taking the answer on trust.

This endpoint is read-only and public, like /api/jobs: it writes nothing
to the database. Abuse protection is deliberately boring and server-side:
- per-IP rate limit (10 requests / 60 seconds, in-memory; the dashboard
  runs a single uvicorn worker, so this is sufficient),
- messages truncated to 500 characters,
- the visitor's text is placed ONLY in the user slot of the LLM payload,
  never in the system instructions, and the model runs at temperature 0
  with a 200-token cap,
- a deterministic scope guard refuses code/general-help requests without
  ever waking the model or the MCP layer.

LLM synthesis is env-gated and OFF by default: it activates only when
MARKET_CHAT_LLM_URL is set (Open WebUI's /api/chat/completions route on
llm.sriomkar.com, authenticated with an API key from an Open WebUI
account's settings). Until that is configured, the drawer answers from
a deterministic template over the same MCP data -- same facts, no
hallucinated prose. An LLM failure never fails the request:
the template answer is returned with llm_error set.

The four-part architecture this module implements:
1. LLM connection profile: the dashboard owns one authenticated
   connection to the model (endpoint + model + API key, all from
   .env; the key never leaves the server). See llm_profile().
2. JSON guardrails: config/market_chat_guardrails.json is the single
   source of truth for scope, refusal text, tool allowlist, and
   limits. The system prompts and the scope guard are built from it.
3. True MCP surface: the model is given the MCP server's own
   JSON-RPC interface -- resources_read (resources/read) and
   tools_call (tools/call) -- discovered live via resources/list
   and tools/list, with a static mirror as fallback. The backend is
   a thin JSON-RPC transport, not a renamed tool layer.
4. Agentic loop: the model picks actions, the backend executes them
   against the read-only reader pool, results go back to the model,
   and only the final answer streams to the drawer.
"""
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from src.core.config import settings

logger = logging.getLogger("de-job-intelligence.market-chat")

# ---------------------------------------------------------------- guardrails
# Static mirror of the MCP server's public surface (src/protocols/
# dashboard_mcp.py PUBLIC_RESOURCES / PUBLIC_TOOLS). Used only when
# live discovery fails; the live list methods are authoritative.
_STATIC_SURFACE = {
    "resources": [
        {
            "uri": "jobs://metrics/overview",
            "name": "Market overview",
            "description": (
                "Aggregate job-market metrics per category: jobs tracked, "
                "average fit score, high-match jobs, hiring companies, and "
                "last ingestion time. No personal or application data."
            ),
            "mimeType": "application/json",
        },
        {
            "uri": "jobs://metrics/top-skills",
            "name": "Top skills in demand",
            "description": (
                "Most frequent skills across tracked postings, counted from "
                "the sanitized skills view. No personal or application data."
            ),
            "mimeType": "application/json",
        },
    ],
    "tools": [
        {
            "name": "query_jobs_by_criteria",
            "description": (
                "Read-only search over sanitized public postings. Filter by "
                "skill and/or minimum fit score. Returns public fields only: "
                "no status, sponsorship, notes, metadata, or application state."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill": {
                        "type": "string",
                        "description": "Skill to match, e.g. Databricks. Case-insensitive.",
                    },
                    "min_score": {
                        "type": "number",
                        "description": "Minimum fit score, 0-100. Defaults to 0.",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum rows, 1-50. Defaults to 20.",
                        "default": 20,
                    },
                },
            },
        },
    ],
}

_DEFAULT_GUARDRAILS = {
    "version": 0,
    "scope": {
        "allowed_topics": ["tracked job postings", "skills in demand", "fit scores"],
        "denied_patterns": [
            r"\b(write|build|create|generate|make me)\b.{0,40}\b(code|script|program|app|game|website|keylogger)\b",
            r"^\s*(write|give me)\s+(a\s+)?(python|java|sql|javascript)\b",
        ],
        "refusal_text": (
            "I can only answer questions about the job market data on "
            "this dashboard (tracked postings, skills in demand, and "
            "fit scores). I can't help with coding or general requests."
        ),
    },
    "grounding": {
        "answer_only_from_tool_results": True,
        "system_prompt": (
            "You are the Job Market Analyst for Sri Omkar's data platform. "
            "Answer the user's question using ONLY the market data below. "
            "If the answer cannot be verified from this data, say so plainly. "
            "Do not invent details, companies, numbers, or trends."
        ),
        "agent_system_prompt": (
            "You are the Job Market Analyst for Sri Omkar's data platform. "
            "You have MCP actions that speak JSON-RPC to the platform's "
            "read-only MCP server. Use them to answer the user's question; "
            "call the actions you need before answering. Answer using ONLY "
            "what the actions return. If the actions do not contain the "
            "answer, say so plainly. Do not invent details, companies, "
            "numbers, or trends."
        ),
    },
    "tools": {
        "allowed_mcp_methods": ["resources/list", "resources/read", "tools/list", "tools/call"],
        "max_tool_rounds": 4,
    },
    "limits": {
        "max_message_chars": 500,
        "max_tokens": 200,
        "rate_limit_per_minute": 10,
        "temperature": 0.0,
    },
}


def _load_guardrails():
    """Load config/market_chat_guardrails.json; fall back to the
    built-in defaults (with a warning) when it is missing or broken,
    so a bad edit never kills the drawer."""
    path = Path(__file__).resolve().parent.parent.parent / "config" / "market_chat_guardrails.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        # Shallow-merge over defaults so a partial file still yields
        # every key the code reads.
        merged = {**_DEFAULT_GUARDRAILS}
        for section in ("scope", "grounding", "tools", "limits"):
            if isinstance(data.get(section), dict):
                merged[section] = {**_DEFAULT_GUARDRAILS[section], **data[section]}
        if "version" in data:
            merged["version"] = data["version"]
        return merged
    except Exception as exc:
        logger.warning("Market-chat guardrails unreadable (%s); using defaults", exc)
        return _DEFAULT_GUARDRAILS


_GUARDRAILS = _load_guardrails()

MAX_MESSAGE_CHARS = int(_GUARDRAILS["limits"]["max_message_chars"])
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX = int(_GUARDRAILS["limits"]["rate_limit_per_minute"])
MCP_TIMEOUT_S = 10
LLM_TIMEOUT_S = 60
LLM_MAX_TOKENS = int(_GUARDRAILS["limits"]["max_tokens"])

SYSTEM_PROMPT = _GUARDRAILS["grounding"]["system_prompt"]
AGENT_SYSTEM_PROMPT = _GUARDRAILS["grounding"]["agent_system_prompt"]
REFUSAL_TEXT = _GUARDRAILS["scope"]["refusal_text"]
MAX_TOOL_ROUNDS = int(_GUARDRAILS["tools"]["max_tool_rounds"])

# Deterministic scope guard, built from the guardrails file: requests
# matching a denied pattern are refused before any MCP or LLM call.
_OFF_TOPIC_PATTERNS = [
    re.compile(pattern) for pattern in _GUARDRAILS["scope"]["denied_patterns"]
]


def llm_profile():
    """The dashboard's LLM connection profile. Describes the
    authenticated connection without ever exposing the key."""
    return {
        "endpoint": settings.MARKET_CHAT_LLM_URL,
        "model": settings.MARKET_CHAT_LLM_MODEL,
        "authenticated": bool(settings.MARKET_CHAT_LLM_KEY),
        "enabled": bool(settings.MARKET_CHAT_LLM_URL),
    }


class _ToolsUnsupported(Exception):
    """The model endpoint rejected tool calling before any tool event
    was emitted, so the drawer falls back to grounded answering."""


# ip -> deque of request timestamps (single-worker in-memory store).
_rate_buckets: dict[str, deque] = {}


def _check_rate_limit(ip: str) -> bool:
    now = time.monotonic()
    bucket = _rate_buckets.setdefault(ip, deque())
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_S:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX:
        return False
    bucket.append(now)
    return True


def _post_json(url, payload, timeout, headers=None):
    """POST a JSON body, return the parsed JSON response. Stdlib only:
    httpx is not a runtime dependency of this project.

    A browser-like User-Agent is mandatory: llm.sriomkar.com sits behind
    Cloudflare, which blocks non-browser signatures (error 1010) before
    the request ever reaches Open WebUI. The internal MCP calls are
    unaffected by the extra header."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _mcp_rpc(method, params):
    """One JSON-RPC call to the public MCP surface. Returns
    (request_payload, response_payload) for the wire trace."""
    request_payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = _post_json(settings.MARKET_CHAT_MCP_URL, request_payload, MCP_TIMEOUT_S)
    return request_payload, response


def _is_off_topic(message: str) -> bool:
    lowered = message.lower()
    return any(pattern.search(lowered) for pattern in _OFF_TOPIC_PATTERNS)


def _normalize_rows(rows, columns):
    """Coerce MCP payload rows to dicts.

    The public MCP surface serializes plain psycopg rows, so a row
    arrives as a JSON array in the SELECT column order, NOT as an
    object. Dict-shaped rows (e.g. a future dict-cursor change) pass
    through untouched. Column orders must match the SQL in
    src/protocols/dashboard_mcp.py exactly.
    """
    normalized = []
    for row in rows or []:
        if isinstance(row, dict):
            normalized.append(row)
        elif isinstance(row, (list, tuple)):
            normalized.append(dict(zip(columns, row)))
    return normalized


_OVERVIEW_COLUMNS = [
    "job_category",
    "total_jobs_tracked",
    "average_fit_score",
    "high_match_jobs",
    "unique_hiring_companies",
    "last_ingested_at",
]
_TOP_SKILL_COLUMNS = ["skill", "job_count"]
_JOB_COLUMNS = [
    "job_id",
    "title",
    "company",
    "location",
    "job_category",
    "fit_score",
    "saved_at",
    "job_url",
    "description_preview",
    "match_score",
]


def _find_skill(message: str, top_skills):
    lowered = message.lower()
    for entry in top_skills:
        skill = (entry.get("skill") or "").lower()
        if skill and skill in lowered:
            return skill
    return None


def _template_answer(message, overview, top_skills, job_rows, skill):
    lines = []
    if overview:
        total = sum(int(row.get("total_jobs_tracked") or 0) for row in overview)
        lines.append(f"Across {total:,} tracked postings:")
        for row in overview:
            lines.append(
                f"- {row.get('job_category')}: {row.get('total_jobs_tracked')} jobs, "
                f"average fit {row.get('average_fit_score')}, "
                f"{row.get('high_match_jobs')} high-match, "
                f"{row.get('unique_hiring_companies')} hiring companies."
            )
    if top_skills:
        skills = ", ".join(
            f"{entry.get('skill')} ({entry.get('job_count')})" for entry in top_skills[:10]
        )
        lines.append(f"Most in-demand skills: {skills}.")
    if skill and job_rows:
        lines.append(f"For '{skill}', {len(job_rows)} top postings by fit:")
        for row in job_rows[:5]:
            lines.append(
                f"- {row.get('title')} at {row.get('company')} "
                f"({row.get('location')}, fit {row.get('fit_score')})."
            )
    elif skill:
        lines.append(f"No postings matched '{skill}' in the current data.")
    if not lines:
        lines.append("No market data is available right now.")
    return "\n".join(lines)


def _llm_payload(message, overview, top_skills, job_rows, stream: bool):
    data = {
        "overview": overview,
        "top_skills": top_skills,
        "matching_jobs": job_rows,
    }
    return {
        "model": settings.MARKET_CHAT_LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Question: {message}\n\nMARKET DATA:\n{json.dumps(data, default=str)}",
            },
        ],
        "stream": stream,
        "temperature": 0.0,
        "max_tokens": LLM_MAX_TOKENS,
    }


def _llm_headers():
    headers = {}
    if settings.MARKET_CHAT_LLM_KEY:
        headers["Authorization"] = f"Bearer {settings.MARKET_CHAT_LLM_KEY}"
    return headers


def _llm_answer(message, overview, top_skills, job_rows):
    """Ask the configured model endpoint. Returns (text, error).

    The endpoint is Open WebUI's OpenAI-compatible chat-completions route
    (https://llm.sriomkar.com/api/chat/completions), authenticated with an
    API key created in an Open WebUI account's settings -- the key acts as
    that account, and it lives only in the jobs VM's .env, never in the
    browser. The response parser also accepts Ollama's native
    /api/chat shape, so the same code works if the URL ever points at
    Ollama directly. Any failure yields (None, 'unavailable') so the
    caller falls back to the template answer."""
    url = settings.MARKET_CHAT_LLM_URL
    if not url:
        return None, None
    payload = _llm_payload(message, overview, top_skills, job_rows, stream=False)
    headers = _llm_headers()
    try:
        response = _post_json(url, payload, LLM_TIMEOUT_S, headers)
        # OpenAI-compatible shape first, Ollama-native shape second.
        choices = response.get("choices") or []
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
        else:
            text = ((response.get("message") or {}).get("content") or "").strip()
        if not text:
            return None, "empty"
        return text, None
    except urllib.error.HTTPError as exc:
        # The body is the only thing that distinguishes a Cloudflare
        # bot block from an Open WebUI auth/model error, so log a
        # snippet of it server-side. It is never returned to the
        # frontend: the drawer only ever sees the generic fallback.
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        logger.warning("Market-chat LLM call failed: HTTP %s %s", exc.code, detail)
        return None, "unavailable"
    except Exception as exc:
        logger.warning("Market-chat LLM call failed: %s", exc)
        return None, "unavailable"


def _gather_market_data(message):
    """Fetch overview + top skills (+ skill-matched jobs) from the public
    MCP surface. Returns (overview, top_skills, job_rows, skill, trace).
    Shared by the JSON and the streaming answers so both stay identical
    in grounding."""
    trace = []
    overview, top_skills, job_rows = [], [], []
    try:
        req, resp = _mcp_rpc("resources/read", {"uri": "jobs://metrics/overview"})
        trace.append({"method": "resources/read", "request": req, "response": resp})
        contents = (resp.get("result") or {}).get("contents") or []
        if contents:
            overview = _normalize_rows(
                json.loads(contents[0].get("text") or "[]"), _OVERVIEW_COLUMNS
            )
    except Exception as exc:
        logger.warning("Market-chat overview fetch failed: %s", exc)
        trace.append({"method": "resources/read", "error": str(exc)[:200]})
    try:
        req, resp = _mcp_rpc("resources/read", {"uri": "jobs://metrics/top-skills"})
        trace.append({"method": "resources/read", "request": req, "response": resp})
        contents = (resp.get("result") or {}).get("contents") or []
        if contents:
            top_skills = _normalize_rows(
                json.loads(contents[0].get("text") or "[]"), _TOP_SKILL_COLUMNS
            )
    except Exception as exc:
        logger.warning("Market-chat top-skills fetch failed: %s", exc)
        trace.append({"method": "resources/read", "error": str(exc)[:200]})

    skill = _find_skill(message, top_skills)
    if skill:
        try:
            req, resp = _mcp_rpc(
                "tools/call",
                {
                    "name": "query_jobs_by_criteria",
                    "arguments": {"skill": skill, "min_score": 0, "limit": 5},
                },
            )
            trace.append({"method": "tools/call", "request": req, "response": resp})
            content = (resp.get("result") or {}).get("content") or []
            if content:
                job_rows = _normalize_rows(
                    json.loads(content[0].get("text") or "[]"), _JOB_COLUMNS
                )
        except Exception as exc:
            logger.warning("Market-chat tool call failed: %s", exc)
            trace.append({"method": "tools/call", "error": str(exc)[:200]})
    return overview, top_skills, job_rows, skill, trace


def answer_question(message: str, client_ip: str) -> dict:
    """Pure logic entry point (testable without HTTP): returns the drawer
    payload with answer, wire trace, and LLM status."""
    message = (message or "").strip()[:MAX_MESSAGE_CHARS]
    if not message:
        return {"answer": "Ask a question about the job market data.", "trace": [], "llm_used": False}
    if _is_off_topic(message):
        return {
            "answer": REFUSAL_TEXT,
            "trace": [],
            "llm_used": False,
            "refused": True,
        }

    overview, top_skills, job_rows, skill, trace = _gather_market_data(message)

    if not overview and not top_skills:
        return {
            "answer": "Market data is temporarily unavailable. Please try again shortly.",
            "trace": trace,
            "llm_used": False,
        }

    template = _template_answer(message, overview, top_skills, job_rows, skill)
    llm_text, llm_error = _llm_answer(message, overview, top_skills, job_rows)
    if llm_text:
        return {"answer": llm_text, "trace": trace, "llm_used": True}
    result = {"answer": template, "trace": trace, "llm_used": False}
    if llm_error:
        result["llm_error"] = llm_error
    return result


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _stream_llm_tokens(messages):
    """Yield raw text deltas from a streaming chat-completions call.

    Open WebUI streams SSE lines: data: {"choices": [{"delta":
    {"content": "..."}}]}, terminated by data: [DONE]. A stall raises
    (urlopen's timeout applies per read), which the caller turns into
    a fallback or a partial answer -- never a hung drawer."""
    url = settings.MARKET_CHAT_LLM_URL
    payload = {
        "model": settings.MARKET_CHAT_LLM_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": 0.0,
        "max_tokens": LLM_MAX_TOKENS,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        **_llm_headers(),
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                return
            try:
                parsed = json.loads(chunk)
            except ValueError:
                continue
            choices = parsed.get("choices") or []
            if choices:
                text = ((choices[0].get("delta") or {}).get("content")) or ""
                if text:
                    yield text


def _discover_mcp_surface():
    """Ask the MCP server what it actually offers (resources/list +
    tools/list). Returns (surface, trace_entries). On any failure,
    falls back to the static mirror with an empty trace, so a
    list-method outage never blocks the drawer."""
    try:
        req_r, resp_r = _mcp_rpc("resources/list", {})
        req_t, resp_t = _mcp_rpc("tools/list", {})
        resources = (resp_r.get("result") or {}).get("resources") or []
        tools = (resp_t.get("result") or {}).get("tools") or []
        if resources and tools:
            return {"resources": resources, "tools": tools}, [
                {"method": "resources/list", "request": req_r, "response": resp_r},
                {"method": "tools/list", "request": req_t, "response": resp_t},
            ]
        raise ValueError("empty surface")
    except Exception as exc:
        logger.warning("Market-chat MCP discovery failed (%s); using static surface", exc)
        return _STATIC_SURFACE, []


def _mcp_dispatch_tools(surface):
    """The two JSON-RPC dispatch actions the model is given. This is
    the MCP server's own interface, not a renamed copy: resources_read
    performs a JSON-RPC resources/read, tools_call performs a JSON-RPC
    tools/call. Enums come from live discovery, so the model can only
    ask for URIs and tools the server actually offers."""
    resource_uris = [r.get("uri") for r in surface.get("resources") or []]
    tool_names = [t.get("name") for t in surface.get("tools") or []]
    return [
        {
            "type": "function",
            "function": {
                "name": "resources_read",
                "description": (
                    "Perform a JSON-RPC resources/read against the MCP server. "
                    "Returns the resource's JSON text."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string", "enum": resource_uris},
                    },
                    "required": ["uri"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tools_call",
                "description": (
                    "Perform a JSON-RPC tools/call against the MCP server. "
                    "Returns the tool's JSON text."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": tool_names},
                        "arguments": {"type": "object"},
                    },
                    "required": ["name"],
                },
            },
        },
    ]


def _execute_mcp_action(name, arguments, trace, surface):
    """Run one model-chosen MCP action by forwarding it as JSON-RPC to
    the MCP server. Returns the text handed back to the model. Unknown
    URIs/names and tool failures become text too, so the model can
    recover instead of the request dying. The backend adds no data of
    its own: it is a transport plus trace, not a second tool layer."""
    if name == "resources_read":
        uri = (arguments or {}).get("uri") or ""
        if uri not in [r.get("uri") for r in surface.get("resources") or []]:
            return f"Unknown resource uri: {uri}"
        mcp_method, params = "resources/read", {"uri": uri}
    elif name == "tools_call":
        tool_name = (arguments or {}).get("name") or ""
        if tool_name not in [t.get("name") for t in surface.get("tools") or []]:
            return f"Unknown tool: {tool_name}"
        mcp_method, params = "tools/call", {
            "name": tool_name,
            "arguments": (arguments or {}).get("arguments") or {},
        }
    else:
        return f"Unknown MCP action: {name}"
    try:
        req, resp = _mcp_rpc(mcp_method, params)
        trace.append({"method": mcp_method, "request": req, "response": resp})
        result = resp.get("result") or {}
        contents = result.get("contents") or result.get("content") or []
        if contents:
            return contents[0].get("text") or ""
        return "No data returned."
    except Exception as exc:
        logger.warning("Market-chat MCP action failed (%s): %s", name, exc)
        trace.append({"method": mcp_method, "error": str(exc)[:200]})
        return f"Tool error: {exc}"


def _agent_stream_events(message):
    """Agentic loop on the true MCP surface: discover what the server
    offers, give the model the JSON-RPC dispatch actions, let the
    model pick actions, forward them verbatim as JSON-RPC, return
    results to the model, and stream only the final answer to the
    drawer. Yields SSE strings. Raises _ToolsUnsupported only when
    the very first tools call fails, so the caller can fall back to
    grounded answering before anything was emitted; later failures
    degrade to answering from whatever the model already gathered."""
    surface, trace = _discover_mcp_surface()
    logger.info("Market-chat LLM profile: %s", llm_profile())
    messages = [
        {
            "role": "system",
            "content": (
                AGENT_SYSTEM_PROMPT
                + "\n\nMCP SURFACE (live from the server):\n"
                + json.dumps(surface, default=str)
            ),
        },
        {"role": "user", "content": message},
    ]
    tools = _mcp_dispatch_tools(surface)
    url = settings.MARKET_CHAT_LLM_URL
    tool_calls_made = 0
    for round_no in range(MAX_TOOL_ROUNDS):
        payload = {
            "model": settings.MARKET_CHAT_LLM_MODEL,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
            "temperature": 0.0,
            "max_tokens": LLM_MAX_TOKENS,
        }
        try:
            resp = _post_json(url, payload, LLM_TIMEOUT_S, _llm_headers())
        except Exception as exc:
            if round_no == 0:
                raise _ToolsUnsupported(str(exc)) from exc
            logger.warning("Market-chat agent round %d failed: %s", round_no, exc)
            break
        msg = ((resp.get("choices") or [{}])[0].get("message") or {})
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break
        messages.append(
            {
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": tool_calls,
            }
        )
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            yield _sse({"type": "tool_call", "tool": name, "arguments": args})
            result_text = _execute_mcp_action(name, args, trace, surface)
            tool_calls_made += 1
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": result_text,
                }
            )
    else:
        # Model never stopped calling tools: force a final answer.
        messages.append(
            {
                "role": "user",
                "content": (
                    "Answer the original question now using only the tool "
                    "results above. Do not call further tools."
                ),
            }
        )
    logger.info("Market-chat agent loop finished with %d trace entries", len(trace))
    if tool_calls_made == 0:
        # The model answered without touching the MCP surface. Its
        # reply would be an empty-context guess: live, this produced
        # hedged "not provided" answers and invented skill counts
        # (Python 1234, SQL 987) copied from the surface listing.
        # Answer from pre-fetched dashboard data instead, so the
        # drawer never presents an ungrounded number as market data.
        logger.info("Market-chat agent made no tool calls; falling back to grounded data")
        yield from _grounded_stream_events(message)
        return
    streamed_any = False
    stream_clean = False
    try:
        for token in _stream_llm_tokens(messages):
            streamed_any = True
            yield _sse({"type": "token", "text": token})
        stream_clean = True
    except Exception as exc:
        logger.warning("Market-chat agent final stream failed: %s", exc)
    if streamed_any:
        yield _sse({"type": "trace", "trace": trace})
        done = {"type": "done", "llm_used": True}
        if not stream_clean:
            done["llm_error"] = "unavailable"
        yield _sse(done)
        return
    # The final stream produced nothing: answer from grounded data
    # instead of leaving the visitor with an error. This also covers
    # the case where the model never called tools and the stream died.
    # (Grounded yields its own trace and done events.)
    yield from _grounded_stream_events(message)


def _grounded_stream_events(message):
    """Previous behavior, kept as the fallback: backend pre-fetches
    fixed MCP data and streams a model summary grounded in it (or the
    template when the model is off)."""
    overview, top_skills, job_rows, skill, trace = _gather_market_data(message)
    if not overview and not top_skills:
        yield _sse({
            "type": "token",
            "text": "Market data is temporarily unavailable. Please try again shortly.",
        })
        yield _sse({"type": "trace", "trace": trace})
        yield _sse({"type": "done", "llm_used": False})
        return
    template = _template_answer(message, overview, top_skills, job_rows, skill)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Question: {message}\n\nMARKET DATA:\n"
                f"{json.dumps({'overview': overview, 'top_skills': top_skills, 'matching_jobs': job_rows}, default=str)}"
            ),
        },
    ]
    streamed_any = False
    stream_clean = False
    if settings.MARKET_CHAT_LLM_URL:
        try:
            for token in _stream_llm_tokens(messages):
                streamed_any = True
                yield _sse({"type": "token", "text": token})
            stream_clean = True
        except Exception as exc:
            logger.warning("Market-chat grounded stream failed: %s", exc)
    if streamed_any:
        yield _sse({"type": "trace", "trace": trace})
        done = {"type": "done", "llm_used": True}
        if not stream_clean:
            done["llm_error"] = "unavailable"
        yield _sse(done)
        return
    for word in template.split(" "):
        yield _sse({"type": "token", "text": word + " "})
    yield _sse({"type": "trace", "trace": trace})
    yield _sse({"type": "done", "llm_used": False, "llm_error": "unavailable"})


def stream_answer(message: str):
    """SSE event generator for the drawer: token events as text arrives,
    then one trace event, then a done event with LLM status. Token text
    is plain data; the frontend appends it via textContent, never HTML.

    With a model endpoint configured, the model drives: it picks MCP
    tools (_agent_stream_events) and only falls back to backend
    pre-fetching (_grounded_stream_events) when the endpoint rejects
    tool calling outright or the model answers without calling any
    tool. Without an endpoint, the grounded template path runs
    directly."""
    message = (message or "").strip()[:MAX_MESSAGE_CHARS]
    if not message:
        yield _sse({"type": "token", "text": "Ask a question about the job market data."})
        yield _sse({"type": "done", "llm_used": False})
        return
    if _is_off_topic(message):
        yield _sse({"type": "token", "text": REFUSAL_TEXT})
        yield _sse({"type": "done", "llm_used": False, "refused": True})
        return
    if not settings.MARKET_CHAT_LLM_URL:
        yield from _grounded_stream_events(message)
        return
    try:
        yield from _agent_stream_events(message)
    except _ToolsUnsupported:
        logger.info("Market-chat: endpoint rejected tools; falling back to grounded answering")
        yield from _grounded_stream_events(message)


async def _parse_message(request: Request):
    """Shared preamble for both chat endpoints. Returns
    (message, error_response_or_None)."""
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return None, JSONResponse(
            {"error": "Too many questions. Please wait a minute and try again."},
            status_code=429,
        )
    try:
        payload = await request.json()
    except Exception:
        return None, JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    message = payload.get("message", "")
    if not isinstance(message, str):
        return None, JSONResponse({"error": "message must be a string."}, status_code=400)
    return message, None


async def market_chat_view(request: Request) -> JSONResponse:
    message, error = await _parse_message(request)
    if error is not None:
        return error
    return JSONResponse(answer_question(message, "unknown"))


async def market_chat_stream_view(request: Request):
    message, error = await _parse_message(request)
    if error is not None:
        return error
    return StreamingResponse(
        stream_answer(message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
