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
"""
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import deque

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from src.core.config import settings

logger = logging.getLogger("de-job-intelligence.market-chat")

MAX_MESSAGE_CHARS = 500
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX = 10
MCP_TIMEOUT_S = 10
LLM_TIMEOUT_S = 60
LLM_MAX_TOKENS = 200

SYSTEM_PROMPT = (
    "You are the Job Market Analyst for Sri Omkar's data platform. "
    "Answer the user's question using ONLY the market data below. "
    "If the answer cannot be verified from this data, say so plainly. "
    "Do not invent details, companies, numbers, or trends."
)

# Agentic drawer: the model itself picks MCP tools based on the question;
# the dashboard backend only executes the calls against the public MCP
# surface (read-only reader pool) and returns results to the model.
AGENT_SYSTEM_PROMPT = (
    "You are the Job Market Analyst for Sri Omkar's data platform. "
    "You have read-only tools that query the tracked job market database: "
    "a market overview, top skills, and jobs matching criteria. "
    "Use the tools to answer the user's question; call the tools you need "
    "before answering. Answer using ONLY what the tools return. "
    "If the tools do not contain the answer, say so plainly. "
    "Do not invent details, companies, numbers, or trends."
)

_MCP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_market_overview",
            "description": (
                "Total tracked postings, average fit, high-match counts, "
                "and hiring-company counts per job category."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_skills",
            "description": "The most frequent skills across tracked postings, with job counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_jobs_by_criteria",
            "description": "Top postings matching a skill, optionally filtered by minimum fit score.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill to match, e.g. python."},
                    "min_score": {"type": "number", "description": "Minimum fit score 0-100."},
                    "limit": {"type": "integer", "description": "Max postings, 1-50."},
                },
            },
        },
    },
]

MAX_TOOL_ROUNDS = 4


class _ToolsUnsupported(Exception):
    """The model endpoint rejected tool calling before any tool event
    was emitted, so the drawer falls back to grounded answering."""

# Deterministic scope guard: code and general-assistant requests are
# refused before any MCP or LLM call. Kept narrow on purpose so real
# market questions ("how do I break into data engineering?") still pass.
_OFF_TOPIC_PATTERNS = [
    re.compile(r"\b(write|build|create|generate|make me)\b.{0,40}\b(code|script|program|app|game|website|keylogger)\b"),
    re.compile(r"^\s*(write|give me)\s+(a\s+)?(python|java|sql|javascript)\b"),
]

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
            "answer": (
                "I can only answer questions about the job market data on "
                "this dashboard (tracked postings, skills in demand, and "
                "fit scores). I can't help with coding or general requests."
            ),
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


def _execute_mcp_tool(name, arguments, trace):
    """Run one model-chosen tool against the public MCP surface.
    Returns the text handed back to the model. Tool failures become
    text too, so the model can recover instead of the request dying.
    Trace entries keep the same shape as the pre-fetch path, so the
    drawer renders model-triggered calls identically."""
    if name == "get_market_overview":
        mcp_method, params = "resources/read", {"uri": "jobs://metrics/overview"}
    elif name == "get_top_skills":
        mcp_method, params = "resources/read", {"uri": "jobs://metrics/top-skills"}
    elif name == "query_jobs_by_criteria":
        mcp_method, params = "tools/call", {
            "name": "query_jobs_by_criteria",
            "arguments": arguments,
        }
    else:
        return f"Unknown tool: {name}"
    try:
        req, resp = _mcp_rpc(mcp_method, params)
        trace.append({"method": mcp_method, "request": req, "response": resp})
        result = resp.get("result") or {}
        contents = result.get("contents") or result.get("content") or []
        if contents:
            return contents[0].get("text") or ""
        return "No data returned."
    except Exception as exc:
        logger.warning("Market-chat tool execution failed (%s): %s", name, exc)
        trace.append({"method": mcp_method, "error": str(exc)[:200]})
        return f"Tool error: {exc}"


def _agent_stream_events(message):
    """Agentic loop: the model picks MCP tools, the backend executes
    them, results go back to the model, and only the final answer
    streams to the drawer. Yields SSE strings. Raises
    _ToolsUnsupported only when the very first tools call fails, so
    the caller can fall back to grounded answering before anything
    was emitted; later failures degrade to answering from whatever
    the model already gathered."""
    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    trace = []
    url = settings.MARKET_CHAT_LLM_URL
    for round_no in range(MAX_TOOL_ROUNDS):
        payload = {
            "model": settings.MARKET_CHAT_LLM_MODEL,
            "messages": messages,
            "tools": _MCP_TOOLS,
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
            result_text = _execute_mcp_tool(name, args, trace)
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
    logger.info("Market-chat agent loop finished with %d tool calls", len(trace))
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
    if settings.MARKET_CHAT_LLM_URL:
        try:
            for token in _stream_llm_tokens(messages):
                streamed_any = True
                yield _sse({"type": "token", "text": token})
        except Exception as exc:
            logger.warning("Market-chat grounded stream failed: %s", exc)
    if streamed_any:
        yield _sse({"type": "trace", "trace": trace})
        yield _sse({"type": "done", "llm_used": True})
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
    tool calling outright. Without an endpoint, the grounded template
    path runs directly."""
    message = (message or "").strip()[:MAX_MESSAGE_CHARS]
    if not message:
        yield _sse({"type": "token", "text": "Ask a question about the job market data."})
        yield _sse({"type": "done", "llm_used": False})
        return
    if _is_off_topic(message):
        yield _sse({
            "type": "token",
            "text": (
                "I can only answer questions about the job market data on "
                "this dashboard (tracked postings, skills in demand, and "
                "fit scores). I can't help with coding or general requests."
            ),
        })
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
