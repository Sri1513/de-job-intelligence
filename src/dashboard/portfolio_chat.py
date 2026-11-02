# src/dashboard/portfolio_chat.py
"""
Portfolio AI assistant backend for sriomkar.com.

Same four-part architecture as the dashboard's market chat
(src/dashboard/market_chat.py), scoped to a different surface:

1. LLM connection profile: the dashboard owns one authenticated
   connection to the model (endpoint + model + API key, all from
   .env via the existing MARKET_CHAT_LLM_URL / MARKET_CHAT_LLM_KEY
   settings; the key never leaves the server and never appears in
   browser code or git).
2. JSON guardrails: config/portfolio_chat_guardrails.json is the
   single source of truth for scope, refusal text, tool allowlist,
   limits, and CORS origins.
3. Tools/resources live in the MCP server; the model gets JSON-RPC
   only (resources_read over resources/read), and the backend
   validates every call against the discovered surface.
4. Agentic loop: the model picks actions, the backend executes
   them against the read-only public MCP surface, results go back
   to the model, and only the final answer streams to the drawer.

SCOPE ISOLATION -- read this before touching this file:
The portfolio route's tool surface allowlist is ONLY
profile://frameworks. _portfolio_surface() filters live discovery
down to that one resource and drops every tool, so the jobs
resources (jobs://metrics/overview, jobs://metrics/top-skills)
and the jobs tool (query_jobs_by_criteria) are never offered to
the model. _execute_portfolio_action() validates again against
the filtered surface, so even a model that hallucinates a jobs
URI or a tools_call action gets a text refusal, never an MCP
call. The market-chat module is untouched: this filter lives
entirely in the portfolio route.

Grounded fallback from day one: the VM2 3B model via Open WebUI
has PROVEN to emit zero tool calls in production (market-chat
commit d29c650). When this loop ends with zero tool calls
executed, the answer comes from pre-fetched profile data, not
from an empty-context model guess. The agentic loop is NOT
verified until a live trace shows resources/read entries.

CORS: sriomkar.com calls this route cross-origin. Responses
carry Access-Control-Allow-Origin for exactly the origins in
the guardrails file -- never a wildcard -- and OPTIONS
preflights are answered here. No other origin gets the header.

Abuse protection mirrors market-chat: per-IP rate limit
(10 requests / 60 seconds, in-memory, single uvicorn worker),
messages truncated to 500 characters, visitor text placed ONLY
in the user slot, temperature 0, 200-token cap, and a
deterministic scope guard that refuses off-profile requests
without waking the model or the MCP layer.
"""
import json
import logging
import re
import time
import urllib.error
from collections import deque
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from src.core.config import settings
from src.dashboard.market_chat import (
    _mcp_rpc,
    _post_json,
    _stream_llm_tokens,
    _ToolsUnsupported,
)

logger = logging.getLogger("de-job-intelligence.portfolio-chat")

PROFILE_URI = "profile://frameworks"

_DEFAULT_GUARDRAILS = {
    "version": 0,
    "scope": {
        "allowed_topics": [
            "Omkar's professional experience",
            "Omkar's projects",
            "Omkar's skills and frameworks",
            "Omkar's education",
            "Omkar's public contact links",
        ],
        "denied_patterns": [
            r"\b(write|build|create|generate|make me)\b.{0,40}\b(code|script|program|app|game|website|keylogger)\b",
            r"^\s*(write|give me)\s+(a\s+)?(python|java|sql|javascript)\b",
            r"\b(visa|sponsorship|immigration|h-1b|green card|citizenship)\b",
            r"\b(salary|hourly rate|pay rate|compensation|expected pay)\b",
            r"\b(phone number|home address|personal email)\b",
        ],
        "refusal_text": (
            "I can only answer questions about Sri Omkar's public "
            "professional profile -- his experience, projects, skills, "
            "frameworks, and education. I can't help with anything else, "
            "and I won't guess at details not in his profile."
        ),
    },
    "grounding": {
        "answer_only_from_profile_data": True,
        "system_prompt": (
            "You are the Portfolio Assistant for Sri Omkar, a Senior "
            "Data Engineer. Answer the visitor's question using ONLY "
            "the profile data below. If the answer cannot be verified "
            "from this data, say so plainly. Do not invent details, "
            "metrics, employers, or contact information. Refer to Sri "
            "Omkar in the third person. Never claim to be Sri Omkar "
            "or answer as him. If asked who you are, answer in the "
            "first person: 'I am the Portfolio Assistant, an AI "
            "assistant that answers questions about Sri Omkar's "
            "public profile.' The profile data provided is your "
            "complete source; never say you lack access to it or "
            "cannot verify Sri Omkar from it. When asked how to "
            "contact Sri Omkar, list ALL contact methods, each "
            "exactly once, and never repeat a URL."
        ),
        "agent_system_prompt": (
            "You are the Portfolio Assistant for Sri Omkar, a Senior "
            "Data Engineer. You have exactly one MCP action: "
            "resources_read, which reads the single public profile "
            "resource profile://frameworks. Call it before answering. "
            "Answer using ONLY what that resource returns. If the "
            "resource does not contain the answer, say so plainly. "
            "Do not invent details, metrics, employers, or contact "
            "information. Refer to Sri Omkar in the third person. "
            "Never claim to be Sri Omkar or answer as him. If asked "
            "who you are, answer in the first person: 'I am the "
            "Portfolio Assistant, an AI assistant that answers "
            "questions about Sri Omkar's public profile.' The "
            "profile data provided is your complete source; never "
            "say you lack access to it or cannot verify Sri Omkar "
            "from it. When asked how to contact Sri Omkar, list ALL "
            "contact methods, each exactly once, and never repeat "
            "a URL."
        ),
    },
    "tools": {
        "allowed_mcp_methods": ["resources/list", "resources/read"],
        "allowed_resources": [PROFILE_URI],
        "allowed_tools": [],
        "max_tool_rounds": 4,
    },
    "limits": {
        "max_message_chars": 500,
        "max_tokens": 200,
        "rate_limit_per_minute": 10,
        "temperature": 0.0,
    },
    "cors": {
        "allowed_origins": ["https://sriomkar.com", "https://www.sriomkar.com"],
    },
}


def _load_guardrails():
    """Load config/portfolio_chat_guardrails.json; fall back to the
    built-in defaults (with a warning) when it is missing or broken,
    so a bad edit never kills the drawer."""
    path = Path(__file__).resolve().parent.parent.parent / "config" / "portfolio_chat_guardrails.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        merged = {**_DEFAULT_GUARDRAILS}
        for section in ("scope", "grounding", "tools", "limits", "cors"):
            if isinstance(data.get(section), dict):
                merged[section] = {**_DEFAULT_GUARDRAILS[section], **data[section]}
        if "version" in data:
            merged["version"] = data["version"]
        return merged
    except Exception as exc:
        logger.warning("Portfolio-chat guardrails unreadable (%s); using defaults", exc)
        return _DEFAULT_GUARDRAILS


_GUARDRAILS = _load_guardrails()

MAX_MESSAGE_CHARS = int(_GUARDRAILS["limits"]["max_message_chars"])
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX = int(_GUARDRAILS["limits"]["rate_limit_per_minute"])
LLM_TIMEOUT_S = 60
LLM_MAX_TOKENS = int(_GUARDRAILS["limits"]["max_tokens"])

SYSTEM_PROMPT = _GUARDRAILS["grounding"]["system_prompt"]
AGENT_SYSTEM_PROMPT = _GUARDRAILS["grounding"]["agent_system_prompt"]
REFUSAL_TEXT = _GUARDRAILS["scope"]["refusal_text"]
MAX_TOOL_ROUNDS = int(_GUARDRAILS["tools"]["max_tool_rounds"])
ALLOWED_ORIGINS = list(_GUARDRAILS["cors"]["allowed_origins"])
ALLOWED_RESOURCES = list(_GUARDRAILS["tools"]["allowed_resources"])

_OFF_TOPIC_PATTERNS = [
    re.compile(pattern) for pattern in _GUARDRAILS["scope"]["denied_patterns"]
]

# Static mirror of the portfolio surface: exactly one resource, no
# tools. Used only when live discovery fails; the live list methods
# are authoritative, and _portfolio_surface() filters them the same
# way, so a discovery failure can never widen the surface.
_STATIC_SURFACE = {
    "resources": [
        {
            "uri": PROFILE_URI,
            "name": "Omkar's public profile: frameworks, experience, projects",
            "description": (
                "Curated public profile data for Sri Omkar: experience, "
                "projects, frameworks, skills, and education. Public-safe "
                "by construction."
            ),
            "mimeType": "application/json",
        }
    ],
    "tools": [],
}

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


def _is_off_topic(message: str) -> bool:
    lowered = message.lower()
    return any(pattern.search(lowered) for pattern in _OFF_TOPIC_PATTERNS)


def _cors_headers(request: Request) -> dict:
    """Allow-Origin for exactly the guardrailed origins, else nothing.
    Never a wildcard: the browser must not be able to read this route
    from an arbitrary site."""
    origin = request.headers.get("origin")
    if origin in ALLOWED_ORIGINS:
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}


def llm_profile():
    """The portfolio route's LLM connection profile. Describes the
    authenticated connection without ever exposing the key. It
    shares the market-chat env vars by design: one VM1 backend,
    one model connection."""
    return {
        "endpoint": settings.MARKET_CHAT_LLM_URL,
        "model": settings.MARKET_CHAT_LLM_MODEL,
        "authenticated": bool(settings.MARKET_CHAT_LLM_KEY),
        "enabled": bool(settings.MARKET_CHAT_LLM_URL),
    }


def _portfolio_surface(discovered):
    """Filter ANY discovered surface down to the portfolio allowlist:
    the profile resource only, no tools. This is the isolation
    boundary: jobs URIs and the jobs tool are dropped here, before
    the model ever sees a surface listing."""
    resources = [
        r for r in (discovered or {}).get("resources") or []
        if r.get("uri") in ALLOWED_RESOURCES
    ]
    if not resources:
        # Discovery returned no profile resource (server older than
        # the profile resource, or a listing outage): fall back to
        # the static mirror rather than to an empty or jobs surface.
        resources = _STATIC_SURFACE["resources"]
    return {"resources": resources, "tools": []}


def _discover_portfolio_surface():
    """Ask the MCP server what it offers, then filter to the
    portfolio allowlist. Returns (surface, trace_entries). On any
    failure, falls back to the static mirror with an empty trace."""
    try:
        req_r, resp_r = _mcp_rpc("resources/list", {})
        req_t, resp_t = _mcp_rpc("tools/list", {})
        resources = (resp_r.get("result") or {}).get("resources") or []
        tools = (resp_t.get("result") or {}).get("tools") or []
        if resources or tools:
            return _portfolio_surface({"resources": resources, "tools": tools}), [
                {"method": "resources/list", "request": req_r, "response": resp_r},
                {"method": "tools/list", "request": req_t, "response": resp_t},
            ]
        raise ValueError("empty surface")
    except Exception as exc:
        logger.warning("Portfolio-chat MCP discovery failed (%s); using static surface", exc)
        return _STATIC_SURFACE, []


def _portfolio_dispatch_tools(surface):
    """The ONE JSON-RPC dispatch action the model is given:
    resources_read, with its uri enum restricted to the filtered
    surface. tools_call is deliberately NOT offered on this route:
    the profile resource needs no tool, and offering tools_call
    would let the model reach the jobs tool."""
    resource_uris = [r.get("uri") for r in surface.get("resources") or []]
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
    ]


def _execute_portfolio_action(name, arguments, trace, surface):
    """Run one model-chosen MCP action, validated against the
    FILTERED surface. Unknown actions, jobs URIs, and tools all
    become text -- never an MCP call -- so the model can recover
    instead of the request dying, and isolation holds even if the
    model hallucinates an action it was never offered."""
    if name != "resources_read":
        return f"Unknown MCP action: {name}"
    uri = (arguments or {}).get("uri") or ""
    if uri not in [r.get("uri") for r in surface.get("resources") or []]:
        return f"Unknown resource uri: {uri}"
    try:
        req, resp = _mcp_rpc("resources/read", {"uri": uri})
        trace.append({"method": "resources/read", "request": req, "response": resp})
        contents = (resp.get("result") or {}).get("contents") or []
        if contents:
            return contents[0].get("text") or ""
        return "No data returned."
    except Exception as exc:
        logger.warning("Portfolio-chat MCP action failed (%s): %s", name, exc)
        trace.append({"method": "resources/read", "error": str(exc)[:200]})
        return f"Tool error: {exc}"


def _gather_profile_data():
    """Fetch the profile resource from the public MCP surface.
    Returns (profile_dict, trace). Only ever reads
    profile://frameworks -- never a jobs URI."""
    trace = []
    profile = {}
    try:
        req, resp = _mcp_rpc("resources/read", {"uri": PROFILE_URI})
        trace.append({"method": "resources/read", "request": req, "response": resp})
        contents = (resp.get("result") or {}).get("contents") or []
        if contents:
            profile = json.loads(contents[0].get("text") or "{}")
    except Exception as exc:
        logger.warning("Portfolio-chat profile fetch failed: %s", exc)
        trace.append({"method": "resources/read", "error": str(exc)[:200]})
    return profile, trace


def _template_answer(message, profile):
    """Deterministic answer built ONLY from the profile dict. Used
    when the model is off and as the grounded fallback when the
    agent loop makes zero tool calls."""
    if not profile:
        return "Profile data is temporarily unavailable. Please try again shortly."
    lines = []
    summary = profile.get("summary")
    if summary:
        lines.append(summary)
    for entry in profile.get("experience") or []:
        employer = entry.get("employer")
        where = f"{employer} (client: {entry.get('client')})" if employer else (entry.get("client") or "")
        lines.append(f"- {entry.get('role')} at {where}, {entry.get('period')}.")
        for highlight in entry.get("highlights") or []:
            lines.append(f"  * {highlight}")
    skills = profile.get("skills") or []
    if skills:
        lines.append(f"Skills: {', '.join(skills)}.")
    for entry in profile.get("frameworks") or []:
        lines.append(f"- Framework: {entry.get('name')} -- {entry.get('description')}")
    for entry in profile.get("projects") or []:
        lines.append(f"- Project: {entry.get('name')} -- {entry.get('description')}")
    for entry in profile.get("education") or []:
        lines.append(f"- Education: {entry.get('degree')}, {entry.get('school')}.")
    contact = profile.get("contact") or {}
    links = [f"{key}: {value}" for key, value in contact.items() if value]
    if links:
        lines.append("Contact: " + "; ".join(links) + ".")
    if not lines:
        lines.append("I don't have that detail in Sri Omkar's public profile.")
    return "\n".join(lines)


def _llm_headers():
    headers = {}
    if settings.MARKET_CHAT_LLM_KEY:
        headers["Authorization"] = f"Bearer {settings.MARKET_CHAT_LLM_KEY}"
    return headers


def _llm_answer(message, profile):
    """Ask the configured model endpoint (non-streaming). Returns
    (text, error). Any failure yields (None, 'unavailable') so the
    caller falls back to the template answer."""
    url = settings.MARKET_CHAT_LLM_URL
    if not url:
        return None, None
    payload = {
        "model": settings.MARKET_CHAT_LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Question: {message}\n\nPROFILE DATA:\n"
                    f"{json.dumps(profile, default=str)}"
                ),
            },
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": LLM_MAX_TOKENS,
    }
    try:
        response = _post_json(url, payload, LLM_TIMEOUT_S, _llm_headers())
        choices = response.get("choices") or []
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
        else:
            text = ((response.get("message") or {}).get("content") or "").strip()
        if not text:
            return None, "empty"
        return text, None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        logger.warning("Portfolio-chat LLM call failed: HTTP %s %s", exc.code, detail)
        return None, "unavailable"
    except Exception as exc:
        logger.warning("Portfolio-chat LLM call failed: %s", exc)
        return None, "unavailable"


def answer_question(message: str, client_ip: str) -> dict:
    """Pure logic entry point (testable without HTTP): returns the
    drawer payload with answer, wire trace, and LLM status."""
    message = (message or "").strip()[:MAX_MESSAGE_CHARS]
    if not message:
        return {"answer": "Ask a question about Sri Omkar's profile.", "trace": [], "llm_used": False}
    if _is_off_topic(message):
        return {
            "answer": REFUSAL_TEXT,
            "trace": [],
            "llm_used": False,
            "refused": True,
        }
    profile, trace = _gather_profile_data()
    if not profile:
        return {
            "answer": "Profile data is temporarily unavailable. Please try again shortly.",
            "trace": trace,
            "llm_used": False,
        }
    template = _template_answer(message, profile)
    llm_text, llm_error = _llm_answer(message, profile)
    if llm_text:
        return {"answer": llm_text, "trace": trace, "llm_used": True}
    result = {"answer": template, "trace": trace, "llm_used": False}
    if llm_error:
        result["llm_error"] = llm_error
    return result


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _grounded_stream_events(message):
    """Backend pre-fetches the profile resource and streams a model
    summary grounded in it (or the template when the model is off).
    This is the EXPECTED launch path, not an error path: the VM2 3B
    model via Open WebUI has proven to emit zero tool calls."""
    profile, trace = _gather_profile_data()
    if not profile:
        yield _sse({
            "type": "token",
            "text": "Profile data is temporarily unavailable. Please try again shortly.",
        })
        yield _sse({"type": "trace", "trace": trace})
        yield _sse({"type": "done", "llm_used": False})
        return
    template = _template_answer(message, profile)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Question: {message}\n\nPROFILE DATA:\n"
                f"{json.dumps(profile, default=str)}"
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
            logger.warning("Portfolio-chat grounded stream failed: %s", exc)
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


def _agent_stream_events(message):
    """Agentic loop on the FILTERED portfolio surface: discover,
    filter to profile://frameworks only, give the model the single
    resources_read action, forward its calls verbatim as JSON-RPC,
    and stream only the final answer. Yields SSE strings. Raises
    _ToolsUnsupported only when the very first tools call fails,
    so the caller can fall back to grounded answering before
    anything was emitted."""
    surface, trace = _discover_portfolio_surface()
    logger.info("Portfolio-chat LLM profile: %s", llm_profile())
    messages = [
        {
            "role": "system",
            "content": (
                AGENT_SYSTEM_PROMPT
                + "\n\nMCP SURFACE (live from the server, portfolio route only):\n"
                + json.dumps(surface, default=str)
            ),
        },
        {"role": "user", "content": message},
    ]
    tools = _portfolio_dispatch_tools(surface)
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
            logger.warning("Portfolio-chat agent round %d failed: %s", round_no, exc)
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
            # Only a validated resources_read against the filtered
            # surface counts as a grounded tool call: a hallucinated
            # jobs URI or tools_call action must not let the loop
            # finish on an empty-context answer.
            surface_uris = [r.get("uri") for r in surface.get("resources") or []]
            if name == "resources_read" and (args or {}).get("uri") in surface_uris:
                tool_calls_made += 1
            result_text = _execute_portfolio_action(name, args, trace, surface)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": result_text,
                }
            )
    else:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Answer the original question now using only the tool "
                    "results above. Do not call further tools."
                ),
            }
        )
    logger.info("Portfolio-chat agent loop finished with %d trace entries", len(trace))
    if tool_calls_made == 0:
        # Expected launch path (see module docstring): answer from
        # pre-fetched profile data instead of streaming an
        # empty-context guess.
        logger.info("Portfolio-chat agent made no tool calls; falling back to grounded data")
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
        logger.warning("Portfolio-chat agent final stream failed: %s", exc)
    if streamed_any:
        yield _sse({"type": "trace", "trace": trace})
        done = {"type": "done", "llm_used": True}
        if not stream_clean:
            done["llm_error"] = "unavailable"
        yield _sse(done)
        return
    yield from _grounded_stream_events(message)


def stream_answer(message: str):
    """SSE event generator for the drawer: token events as text arrives,
    then one trace event, then a done event with LLM status. Token text
    is plain data; the frontend appends it via textContent, never HTML."""
    message = (message or "").strip()[:MAX_MESSAGE_CHARS]
    if not message:
        yield _sse({"type": "token", "text": "Ask a question about Sri Omkar's profile."})
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
        logger.info("Portfolio-chat: endpoint rejected tools; falling back to grounded answering")
        yield from _grounded_stream_events(message)


async def _parse_message(request: Request):
    """Shared preamble for both chat endpoints. Returns
    (message, error_response_or_None). Error responses carry the
    same CORS headers as success responses, so the browser can
    read them cross-origin."""
    cors = _cors_headers(request)
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return None, JSONResponse(
            {"error": "Too many questions. Please wait a minute and try again."},
            status_code=429,
            headers=cors,
        )
    try:
        payload = await request.json()
    except Exception:
        return None, JSONResponse({"error": "Invalid JSON body."}, status_code=400, headers=cors)
    message = payload.get("message", "")
    if not message:
        # The portfolio drawer originally posted {"question": ...};
        # accept it as a fallback so cached pages keep working.
        message = payload.get("question", "")
    if not isinstance(message, str):
        return None, JSONResponse({"error": "message must be a string."}, status_code=400, headers=cors)
    return message, None


async def portfolio_chat_view(request: Request) -> JSONResponse:
    message, error = await _parse_message(request)
    if error is not None:
        return error
    return JSONResponse(answer_question(message, "unknown"), headers=_cors_headers(request))


async def portfolio_chat_stream_view(request: Request):
    message, error = await _parse_message(request)
    if error is not None:
        return error
    return StreamingResponse(
        stream_answer(message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            **_cors_headers(request),
        },
    )


async def portfolio_chat_preflight(request: Request) -> Response:
    """OPTIONS preflight for the cross-origin portfolio drawer. Only
    guardrailed origins get an Allow-Origin; anything else gets a
    bare 204 the browser will refuse to use."""
    headers = {}
    cors = _cors_headers(request)
    if cors:
        headers = {
            **cors,
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        }
    return Response(status_code=204, headers=headers)
