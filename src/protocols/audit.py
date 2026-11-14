# src/protocols/audit.py
"""Per-tool-call audit trail for the MCP server (Sprint 2, Batch 3).

Every tools/call gets one UUID correlation_id, generated at the HTTP
boundary (handle_rpc). The same id ties three things together:

1. The sanitized error response — a failed call returns
   ``{"correlation_id": ...}`` instead of raw exception text, so the
   owner can find the call in the logs without the client ever seeing
   internals.
2. One structured JSON log line per call (tool, scope, client, status,
   duration, error code, client-safe message) on the dedicated
   ``de-job-intelligence.mcp.audit`` logger. One line, one JSON object —
   greppable with jq, no traceback parsing.
3. One row in ``scout.mcp_audit_log`` (migration 007) with the same id.

HARD RULES — audit is observability, never the critical path (same
posture as src/core/llm_telemetry.py):
- FAIL-OPEN: ``record_audit_row`` never raises. DB down, table missing,
  bad values — it logs a warning and returns None. A tool call must
  never fail because its audit write failed.
- NO SECRETS, NO PAYLOADS: only names, timings, and client-safe
  messages are stored. Never pass arguments, responses, tokens, or
  tracebacks in here. ``error_message`` holds the message the client
  already saw, nothing more.
- KILL SWITCH: ``MCP_AUDIT_ENABLED=false`` disables the DB row (reads
  the env var on every call so tests can toggle it). The JSON log line
  still emits — logs are the fallback when the database is the thing
  that is down.
- The audit row records the MCP *call*, not the background job: an
  async pipeline tool (run_batch_ingestion / run_email_pipeline) gets
  its row when dispatch returns "started", with the dispatch duration.
  The background run's own finish/failure stays in the text logs.

Deliberately NOT in this batch (follow-ups, not oversights):
- The scope CHECK on mcp_audit_log.scope that migration 007's header
  anticipates ("the CHECK then"). It needs a new migration file + a
  manual apply on the VM; the row stores the tool's required scope
  (TOOL_SCOPES, default 'mcp') which already fits that vocabulary.
- Threading the correlation_id into scout.llm_telemetry_ledger: the
  ledger accepts it, but passing it through every handler into the
  LLM builders is a cross-cutting change of its own.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from src.core.database import get_pool
from src.protocols.scopes import TOOL_SCOPES

logger = logging.getLogger(__name__)

# Dedicated logger for the one-JSON-object-per-line record. A separate
# logger (not the text-format de-job-intelligence.mcp logger in app.py)
# so the JSON lines stay machine-parseable and the human-readable
# "Tool executed:" lines stay untouched. Own stderr handler, no
# propagation, so records can't double-emit via root.
_audit_logger = logging.getLogger("de-job-intelligence.mcp.audit")
_audit_logger.setLevel(logging.INFO)
if not _audit_logger.handlers:
    _audit_logger.addHandler(logging.StreamHandler())
_audit_logger.propagate = False

_INSERT_SQL = """
    INSERT INTO scout.mcp_audit_log
        (correlation_id, client_id, tool_name, scope, duration_ms,
         status, error_code, error_message, request_size_bytes)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def new_correlation_id() -> str:
    """One UUID per tools/call, as a string."""
    return str(uuid.uuid4())


def client_id_for_request(request) -> Optional[str]:
    """OAuth client_id for this request, or None for the static token.

    The auth middleware stashes ``mcp_client_id`` on the ASGI scope
    (the OAuth token record's client_id; None when the static owner
    token was presented). Absent means the request bypassed the
    middleware — fail closed to None, never guess.
    """
    return request.scope.get("mcp_client_id")


def required_scope_for_tool(tool_name: Optional[str]) -> str:
    """The scope a call to this tool needs (audit column value).

    Single value from TOOL_SCOPES; 'mcp' for unknown tools (matches
    the column default and the scope CHECK vocabulary migration 007
    anticipates).
    """
    if not tool_name:
        return "mcp"
    return TOOL_SCOPES.get(tool_name, "mcp")


def build_log_record(
    *,
    correlation_id: str,
    tool_name: str,
    scope: str,
    client_id: Optional[str],
    status: str,
    duration_ms: Optional[int],
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    request_size_bytes: Optional[int] = None,
) -> dict:
    """Pure builder for the one-JSON-object-per-call record.

    Kept pure so tests can assert the shape without touching logging.
    ``error_message`` must already be client-safe (the message the
    client saw) — never pass exception text here.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "INFO" if status == "ok" else "ERROR",
        "event": "tool_call",
        "correlation_id": correlation_id,
        "tool_name": tool_name,
        "scope": scope,
        "client_id": client_id,
        "status": status,
        "duration_ms": duration_ms,
        "error_code": error_code,
        "error_message": error_message,
        "request_size_bytes": request_size_bytes,
    }


def log_tool_call(**kwargs) -> dict:
    """Emit one JSON line for a tool call; return the record.

    Never raises: a logging failure must not break the call it records.
    """
    try:
        record = build_log_record(**kwargs)
        _audit_logger.info(json.dumps(record, default=str))
        return record
    except Exception as exc:  # noqa: BLE001 - logging must never break calls
        logger.warning("mcp-audit: JSON log emit failed (ignored): %s", exc)
        return {}


def _enabled() -> bool:
    return os.getenv("MCP_AUDIT_ENABLED", "true").strip().lower() not in (
        "false",
        "0",
        "no",
    )


def record_audit_row(
    *,
    correlation_id: str,
    tool_name: str,
    scope: str,
    client_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    status: str = "ok",
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    request_size_bytes: Optional[int] = None,
) -> None:
    """Write one mcp_audit_log row. Never raises (see module docstring)."""
    if not _enabled():
        return
    try:
        pool = get_pool()
        # Bounded wait: pool.connection() with no timeout waits FOREVER
        # for a connection — a dead database would stall every tool call
        # instead of failing open. 2s is plenty for a local INSERT.
        with pool.connection(timeout=2) as conn:
            with conn.cursor() as cur:
                # Bound the write: a stuck INSERT must not stall the caller.
                cur.execute("SET LOCAL statement_timeout = '5s'")
                cur.execute(
                    _INSERT_SQL,
                    (
                        correlation_id,
                        client_id,
                        tool_name,
                        scope,
                        duration_ms,
                        status,
                        error_code,
                        error_message[:500] if error_message else None,
                        request_size_bytes,
                    ),
                )
    except Exception as exc:  # noqa: BLE001 - audit must never break calls
        logger.warning("mcp-audit: ledger write failed (ignored): %s", exc)


async def arecord_audit_row(**kwargs) -> None:
    """Async wrapper for the handle_rpc path.

    Runs the blocking psycopg write in a thread so the event loop is
    never stalled by audit. Still never raises.
    """
    await asyncio.to_thread(record_audit_row, **kwargs)
