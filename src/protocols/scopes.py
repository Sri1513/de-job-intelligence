# src/protocols/scopes.py
"""
Server-side OAuth scopes for MCP tools (Sprint 2, Batch 2).

Before this module, every credential carried the single scope ``mcp`` and
could call any tool: a read-only dashboard client had the same power as
the apply worker. Tools are now split into three scopes:

- ``mcp:read``   — read-only tools (search, get details, tailoring prompt).
- ``mcp:ingest`` — tools that bring data in (ingestion, email pipeline,
  staging, alert drafts, retries).
- ``mcp:apply``  — tools with side effects (queueing, the apply worker,
  exporting a resume to Google Docs).

``mcp`` is the legacy full-access scope: it expands to all three, so
tokens issued before this batch keep working unchanged. New clients
request the granular scopes they need; the owner sees exactly what is
being granted on the approval page.

Enforcement lives in ``dispatch_tool_call`` (the single choke point for
every tool call): it takes the caller's granted scopes and raises
``InsufficientScopeError`` before validation or any handler runs, so a
caller that lacks a scope learns nothing about the tool's arguments.
The scopes themselves are resolved by the auth middleware at request
time — it is the only layer that knows which credential was presented
(the static token can be overridden per app instance, so no other layer
may re-derive them) — and stashed on the ASGI scope;
``scopes_for_request`` reads them back. A direct ``dispatch_tool_call``
with ``scopes=None`` is unrestricted — that path is for internal/direct
callers only, never for HTTP traffic.
"""
from typing import Optional

SCOPE_READ = "mcp:read"
SCOPE_INGEST = "mcp:ingest"
SCOPE_APPLY = "mcp:apply"
SCOPE_LEGACY = "mcp"

ALL_SCOPES = frozenset({SCOPE_READ, SCOPE_INGEST, SCOPE_APPLY})
KNOWN_SCOPES = frozenset({SCOPE_LEGACY, SCOPE_READ, SCOPE_INGEST, SCOPE_APPLY})
SCOPES_SUPPORTED = ["mcp", "mcp:read", "mcp:ingest", "mcp:apply"]

# Tool name -> the scope a caller needs to run it. Aliases share their
# tool's scope. process_job_alert_draft is ingest even though
# generate_assets=true creates a Gmail draft + Doc: the tool's default is
# ingest-only, and the draft is created only on the owner's explicit ask.
TOOL_SCOPES: dict[str, str] = {
    "prepare_job_tailoring_prompt": SCOPE_READ,
    "fetch_job_for_tailoring": SCOPE_READ,
    "get_job_details": SCOPE_READ,
    "search_saved_jobs": SCOPE_READ,
    "run_batch_ingestion": SCOPE_INGEST,
    "run_email_pipeline": SCOPE_INGEST,
    "stage_agent_jobs": SCOPE_INGEST,
    "process_job_alert_draft": SCOPE_INGEST,
    "retry_job_evaluations": SCOPE_INGEST,
    "queue_job_for_application": SCOPE_APPLY,
    "run_application_worker": SCOPE_APPLY,
    "export_tailored_resume": SCOPE_APPLY,
    "generate_tailored_resume": SCOPE_APPLY,
}


class InsufficientScopeError(Exception):
    """A tool call was denied: the caller's scopes lack the tool's scope."""

    def __init__(self, tool_name: str, required_scope: str, granted: frozenset):
        super().__init__(
            f"Insufficient scope: tool '{tool_name}' requires '{required_scope}'"
        )
        self.tool_name = tool_name
        self.required_scope = required_scope
        self.granted = granted


def parse_scopes(scope: Optional[str]) -> frozenset:
    """Effective scopes for a space-separated scope string.

    ``mcp`` expands to all three; unknown names are dropped (the authorize
    and register endpoints reject them explicitly with invalid_scope).
    """
    if not scope:
        return frozenset()
    out = set()
    for name in str(scope).split():
        if name == SCOPE_LEGACY:
            out.update(ALL_SCOPES)
        elif name in ALL_SCOPES:
            out.add(name)
    return frozenset(out)


def unknown_scopes(scope: Optional[str]) -> list:
    """Scope names in the string that are not known scopes."""
    if not scope:
        return []
    return [name for name in str(scope).split() if name not in KNOWN_SCOPES]


def normalize_scope_string(scope: Optional[str]) -> str:
    """Canonical storage form: ``mcp`` when the set is complete, else the
    sorted granular names joined by a space ("" when nothing is granted)."""
    effective = parse_scopes(scope)
    if effective == ALL_SCOPES:
        return SCOPE_LEGACY
    return " ".join(sorted(effective))


def require_tool_scope(tool_name: str, scopes: Optional[frozenset]) -> None:
    """Raise InsufficientScopeError unless `scopes` covers the tool.

    `scopes=None` means "no scope information" (direct/internal caller)
    and is unrestricted. An empty set is authenticated-but-unscoped and
    is denied for every tool. Unknown tools are not scope-checked here;
    dispatch reports them as an error result, unchanged.
    """
    if scopes is None:
        return
    required = TOOL_SCOPES.get(tool_name)
    if required is None:
        return
    if required not in scopes:
        raise InsufficientScopeError(tool_name, required, scopes)


def scopes_for_request(request) -> frozenset:
    """Scopes the auth middleware resolved for this request.

    The middleware stashes them on the ASGI scope as ``mcp_scopes``.
    Absent (the request bypassed the middleware) fails closed to an
    empty set: the caller can authenticate but call no tool.
    """
    scopes = request.scope.get("mcp_scopes")
    if scopes is None:
        return frozenset()
    return scopes
