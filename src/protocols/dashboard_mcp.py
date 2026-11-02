# src/protocols/dashboard_mcp.py
"""
Public, unauthenticated, read-only MCP surface for the dashboard.

This is the second half of the Stage 1/2 database boundary: migration 004
created the ``mcp_dashboard_reader`` role and two sanitized views, and this
module is the ONLY code allowed to serve them publicly. It speaks the same
JSON-RPC 2.0 shape as the private server (src/protocols/app.py) and adds
the two primitives the private server does not have: resources and prompts.

SECURITY BOUNDARY -- read this before touching this file:
- This module imports ONLY get_reader_pool from src/core/database. It must
  never import the shared write pool. A test pins
  this: tests/test_dashboard_mcp_public.py::test_module_never_touches_write_pool.
- The route is exempt from BearerAuthMiddleware by EXACT path
  (see _is_exempt_path in auth.py). The exemption is safe only because
  every query below runs against the reader pool, which holds SELECT on
  exactly three sanitized views and nothing else (migrations 004/005).
- Every query is a fixed string with bound parameters. No caller input is
  ever interpolated into SQL, and no raw-SQL tool exists.
- Server-side caps: result limits are clamped to 50, min_score to 0-100,
  and the role itself enforces a 2-second statement timeout.
- Fail-closed: when MCP_DASHBOARD_READER_PASSWORD is empty, all
  data-serving methods answer 503. Listing methods (which need no DB)
  still work, so clients can discover the surface without data leaking.
- The profile://frameworks resource is the exception to the DB rule: it
  serves the curated, public-safe file config/company_frameworks.json
  (no database, no reader pool, no private data by construction -- the
  file holds only public profile content and is approved separately).
  It needs no reader password, so it stays available when the DB
  methods fail closed.
"""
import json
import logging
from contextlib import contextmanager
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.core.config import settings
from src.core.database import get_reader_pool

logger = logging.getLogger("de-job-intelligence.public-mcp")

PUBLIC_MCP_PATH = "/public/mcp"

SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
TOP_SKILLS_LIMIT = 20

OVERVIEW_URI = "jobs://metrics/overview"
TOP_SKILLS_URI = "jobs://metrics/top-skills"
PROFILE_URI = "profile://frameworks"
PROFILE_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "company_frameworks.json"

PUBLIC_RESOURCES = [
    {
        "uri": OVERVIEW_URI,
        "name": "Market overview",
        "description": (
            "Aggregate job-market metrics per category: jobs tracked, "
            "average fit score, high-match jobs, hiring companies, and "
            "last ingestion time. No personal or application data."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": TOP_SKILLS_URI,
        "name": "Top skills in demand",
        "description": (
            "Most frequent skills across tracked postings, counted from "
            "the sanitized skills view. No personal or application data."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": PROFILE_URI,
        "name": "Omkar's public profile: frameworks, experience, projects",
        "description": (
            "Curated public profile data for Sri Omkar: experience, "
            "projects, frameworks, skills, and education. Public-safe "
            "by construction: no sponsorship, rates, salary, private "
            "notes, or non-public contact details."
        ),
        "mimeType": "application/json",
    },
]

PUBLIC_PROMPTS = [
    {
        "name": "market_skill_demand",
        "description": (
            "Ask about hiring demand for a skill, grounded in live "
            "top-skills data. Optional argument: skill."
        ),
        "arguments": [
            {
                "name": "skill",
                "description": "Skill to focus on, e.g. Databricks",
                "required": False,
            }
        ],
    },
    {
        "name": "explain_fit_score",
        "description": (
            "Explain how fit scores are computed on this platform, "
            "grounded in live aggregate metrics."
        ),
        "arguments": [],
    },
]

PUBLIC_TOOLS = [
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
                    "default": DEFAULT_LIMIT,
                },
            },
        },
    },
]


class _ReaderUnavailable(RuntimeError):
    """Raised when the reader pool cannot serve data (fail-closed)."""


@contextmanager
def _reader_connection():
    """Yield a connection from the reader pool, or fail closed."""
    if not settings.MCP_DASHBOARD_READER_PASSWORD:
        raise _ReaderUnavailable(
            "Public MCP reader is not configured: "
            "MCP_DASHBOARD_READER_PASSWORD is empty."
        )
    pool = get_reader_pool()
    with pool.connection() as conn:
        yield conn


def _rows_to_json(rows):
    return json.loads(json.dumps(rows, default=str))


def _read_profile_data():
    """Read and validate the curated public profile file. Raises
    ValueError when the file is missing or not valid JSON, so the
    handler can answer a JSON-RPC error instead of serving partial
    or stale data."""
    with open(PROFILE_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("profile file must contain a JSON object")
    return data


# ---------------------------------------------------------------- queries
# fit_score is stored as TEXT, so every numeric comparison goes through a
# CASE guard: CASE guarantees the regex check runs before the cast, while
# a plain `AND` in WHERE would let the planner cast first and fail on
# non-numeric values. Same pattern as migration 004's metrics view.


def _fetch_overview(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_category, total_jobs_tracked, average_fit_score, "
            "high_match_jobs, unique_hiring_companies, last_ingested_at "
            "FROM scout.v_dashboard_market_metrics "
            "ORDER BY total_jobs_tracked DESC"
        )
        return _rows_to_json(cur.fetchall())


def _fetch_top_skills(conn, limit=TOP_SKILLS_LIMIT):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT skill, count(*) AS job_count "
            "FROM scout.v_dashboard_job_skills "
            "GROUP BY skill ORDER BY job_count DESC, skill ASC LIMIT %s",
            (limit,),
        )
        return _rows_to_json(cur.fetchall())


def _fetch_skill_count(conn, skill):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS job_count FROM scout.v_dashboard_job_skills "
            "WHERE skill = %s",
            (skill,),
        )
        row = cur.fetchone()
        if not row:
            return 0
        # Same tuple-vs-dict reality as _rows_to_json: the reader cursor
        # returns plain tuples, so accept both shapes.
        return row["job_count"] if isinstance(row, dict) else row[0]


def _query_jobs(conn, skill, min_score, limit):
    # Score expression reused in SELECT and WHERE so ordering and
    # filtering agree, and so no non-numeric fit_score is ever cast.
    score_expr = (
        "CASE WHEN a.fit_score::text ~ '^[0-9]+(\\.[0-9]+)?$' "
        "THEN a.fit_score::numeric ELSE NULL END"
    )
    params = []
    join = ""
    if skill:
        join = (
            " JOIN scout.v_dashboard_job_skills s "
            "ON s.job_id = a.job_id AND s.skill = %s"
        )
        params.append(skill)
    params.extend([min_score, limit])
    sql = (
        "SELECT a.job_id, a.title, a.company, a.location, a.job_category, "
        "a.fit_score, a.saved_at, a.job_url, a.description_preview, "
        f"{score_expr} AS match_score "
        "FROM scout.v_dashboard_job_analytics a "
        f"{join} "
        f"WHERE {score_expr} >= %s "
        "ORDER BY match_score DESC NULLS LAST, a.saved_at DESC NULLS LAST "
        "LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return _rows_to_json(cur.fetchall())


# ------------------------------------------------------------------ args


def _clamp_min_score(value):
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("min_score must be a number")
    return max(0, min(100, value))


def _clamp_limit(value):
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    return max(1, min(MAX_LIMIT, value))


def _clean_skill(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("skill must be a string")
    cleaned = value.strip().lower()
    return cleaned or None


# ---------------------------------------------------------------- handler


def _error(rpc_id, code, message, status_code=200):
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        },
        status_code=status_code,
    )


def _result(rpc_id, result):
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


async def handle_public_rpc(request: Request) -> JSONResponse | Response:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
            status_code=400,
        )

    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {}) or {}

    try:
        if method == "initialize":
            requested_version = params.get("protocolVersion")
            negotiated_version = (
                requested_version
                if requested_version in SUPPORTED_PROTOCOL_VERSIONS
                else DEFAULT_PROTOCOL_VERSION
            )
            return _result(
                rpc_id,
                {
                    "protocolVersion": negotiated_version,
                    "serverInfo": {"name": "de-job-intelligence-public", "version": "1.0.0"},
                    "capabilities": {"resources": {}, "prompts": {}, "tools": {}},
                },
            )

        if method == "notifications/initialized":
            return Response(status_code=202)

        if method == "ping":
            return _result(rpc_id, {})

        if method == "resources/list":
            return _result(rpc_id, {"resources": PUBLIC_RESOURCES})

        if method == "resources/read":
            uri = params.get("uri")
            # The profile resource serves a curated file, not the DB:
            # answer it before touching the reader pool, so it stays
            # available when the DB methods fail closed.
            if uri == PROFILE_URI:
                try:
                    data = _read_profile_data()
                except Exception as exc:
                    return _error(rpc_id, -32603, f"Profile data unavailable: {exc}")
                return _result(
                    rpc_id,
                    {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "application/json",
                                "text": json.dumps(data, default=str),
                            }
                        ]
                    },
                )
            with _reader_connection() as conn:
                if uri == OVERVIEW_URI:
                    data = _fetch_overview(conn)
                elif uri == TOP_SKILLS_URI:
                    data = _fetch_top_skills(conn)
                else:
                    return _error(rpc_id, -32602, f"Unknown resource uri: {uri!r}")
            return _result(
                rpc_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(data, default=str),
                        }
                    ]
                },
            )

        if method == "prompts/list":
            return _result(rpc_id, {"prompts": PUBLIC_PROMPTS})

        if method == "prompts/get":
            name = params.get("name")
            arguments = params.get("arguments", {}) or {}
            with _reader_connection() as conn:
                if name == "market_skill_demand":
                    skill = _clean_skill(arguments.get("skill"))
                    top = _fetch_top_skills(conn, limit=10)
                    focus = (
                        {"skill": skill, "job_count": _fetch_skill_count(conn, skill)}
                        if skill
                        else None
                    )
                    text = (
                        "You are the Job Market Analyst for Sri Omkar's data "
                        "platform. Answer using ONLY the market data below. "
                        "If the answer cannot be verified from this data, say "
                        "so plainly; do not invent details.\n\n"
                        f"TOP SKILLS: {json.dumps(top, default=str)}\n"
                        + (
                            f"FOCUS SKILL: {json.dumps(focus, default=str)}\n"
                            if focus
                            else ""
                        )
                    )
                elif name == "explain_fit_score":
                    overview = _fetch_overview(conn)
                    text = (
                        "You are the Job Market Analyst for Sri Omkar's data "
                        "platform. Explain how fit scores work, using ONLY "
                        "the facts below. Fit scores are computed locally by "
                        "keyword and taxonomy matching (rapidfuzz and "
                        "rank-bm25) against each posting's skills, scaled "
                        "0-100; they are not LLM judgments. If a detail "
                        "cannot be verified from the data below, say so "
                        "plainly; do not invent details.\n\n"
                        f"AGGREGATE METRICS: {json.dumps(overview, default=str)}"
                    )
                else:
                    return _error(rpc_id, -32602, f"Unknown prompt: {name!r}")
            return _result(
                rpc_id,
                {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]},
            )

        if method == "tools/list":
            return _result(rpc_id, {"tools": PUBLIC_TOOLS})

        if method == "tools/call":
            name = params.get("name")
            if name != "query_jobs_by_criteria":
                return _error(rpc_id, -32602, f"Unknown tool: {name!r}")
            arguments = params.get("arguments", {}) or {}
            try:
                skill = _clean_skill(arguments.get("skill"))
                min_score = _clamp_min_score(arguments.get("min_score"))
                limit = _clamp_limit(arguments.get("limit"))
            except ValueError as exc:
                return _error(rpc_id, -32602, str(exc))
            with _reader_connection() as conn:
                rows = _query_jobs(conn, skill, min_score, limit)
            logger.info("Public MCP tool executed: %s", name)
            return _result(
                rpc_id,
                {"content": [{"type": "text", "text": json.dumps(rows, default=str)}]},
            )

        return _error(rpc_id, -32601, f"Method '{method}' not found")
    except _ReaderUnavailable as exc:
        return _error(rpc_id, -32002, str(exc), status_code=503)
    except Exception as exc:
        logger.exception("Public MCP request failed: %s", method)
        return _error(rpc_id, -32603, f"Internal error: {exc}")
