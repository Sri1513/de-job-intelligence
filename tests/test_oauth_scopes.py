"""Tests for Sprint 2, Batch 2: server-side OAuth scopes.

Tools are split into mcp:read / mcp:ingest / mcp:apply; the legacy scope
"mcp" expands to all three. The middleware resolves a credential's scopes
at request time and stashes them on the ASGI scope; dispatch enforces them
before validation or any handler runs. Scope issuance is validated at
register/authorize time, and the approval page shows what is granted.
"""
import base64
import hashlib
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.protocols import dispatcher as dispatcher_module
from src.protocols import token_store as ts_module
from src.protocols.app import create_app
from src.protocols.dispatcher import TOOL_HANDLERS, dispatch_tool_call
from src.protocols.scopes import (
    ALL_SCOPES,
    TOOL_SCOPES,
    InsufficientScopeError,
    normalize_scope_string,
    parse_scopes,
    require_tool_scope,
    unknown_scopes,
)

OWNER_TOKEN = "owner-secret-token"
STATIC_BEARER = "test-bearer"

# RFC 7636 Appendix B test vector (same as tests/test_mcp_oauth.py)
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()

REDIRECT_URI = "https://gemini.example.com/oauth/callback"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ts_module, "_store", None)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", OWNER_TOKEN)
    monkeypatch.setattr(settings, "MCP_PUBLIC_URL", "https://mcp.example.com")
    return TestClient(create_app(auth_token=STATIC_BEARER))


def _register(client, scope=None) -> dict:
    body = {"redirect_uris": [REDIRECT_URI], "client_name": "Test Client"}
    if scope is not None:
        body["scope"] = scope
    response = client.post("/oauth/register", json=body)
    assert response.status_code == 201
    return response.json()


def _approve(client, client_id: str, scope: str = "mcp") -> str:
    """Runs the authorize POST approval; returns the issued code."""
    response = client.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": "xyz",
            "scope": scope,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "owner_token": OWNER_TOKEN,
            "decision": "approve",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query["code"][0]


def _exchange(client, client_id: str, code: str) -> dict:
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert response.status_code == 200
    return response.json()


def _oauth_token(client, scope: str) -> str:
    creds = _register(client)
    code = _approve(client, creds["client_id"], scope=scope)
    pair = _exchange(client, creds["client_id"], code)
    return pair["access_token"]


def _rpc(client, token: str, tool: str, arguments: dict):
    return client.post(
        "/rpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _mock_db(monkeypatch):
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    monkeypatch.setattr(dispatcher_module, "get_db_connection", lambda: cm)


# ---------------------------------------------------------------------------
# Scope parsing / normalization
# ---------------------------------------------------------------------------


def test_parse_scopes_legacy_expands_to_all():
    assert parse_scopes("mcp") == ALL_SCOPES


def test_parse_scopes_granular():
    assert parse_scopes("mcp:read mcp:ingest") == {"mcp:read", "mcp:ingest"}


def test_parse_scopes_drops_unknown_and_empty():
    assert parse_scopes("mcp:read bogus  ") == {"mcp:read"}
    assert parse_scopes("") == frozenset()
    assert parse_scopes(None) == frozenset()


def test_unknown_scopes():
    assert unknown_scopes("mcp:read bogus also-bad") == ["bogus", "also-bad"]
    assert unknown_scopes("mcp mcp:read mcp:ingest mcp:apply") == []


def test_normalize_scope_string():
    assert normalize_scope_string("mcp:ingest mcp:read") == "mcp:ingest mcp:read"
    assert normalize_scope_string("mcp:read mcp:ingest mcp:apply") == "mcp"
    assert normalize_scope_string("mcp") == "mcp"
    assert normalize_scope_string("") == ""


def test_tool_scopes_cover_every_tool():
    assert set(TOOL_SCOPES) == set(TOOL_HANDLERS)
    assert TOOL_SCOPES["search_saved_jobs"] == "mcp:read"
    assert TOOL_SCOPES["stage_agent_jobs"] == "mcp:ingest"
    assert TOOL_SCOPES["queue_job_for_application"] == "mcp:apply"
    assert TOOL_SCOPES["export_tailored_resume"] == "mcp:apply"
    assert TOOL_SCOPES["fetch_job_for_tailoring"] == "mcp:read"
    assert TOOL_SCOPES["generate_tailored_resume"] == "mcp:apply"


# ---------------------------------------------------------------------------
# require_tool_scope
# ---------------------------------------------------------------------------


def test_require_tool_scope_allows_covered():
    require_tool_scope("search_saved_jobs", frozenset({"mcp:read"}))
    require_tool_scope("stage_agent_jobs", frozenset({"mcp:read", "mcp:ingest"}))


def test_require_tool_scope_denies_missing():
    with pytest.raises(InsufficientScopeError) as excinfo:
        require_tool_scope("queue_job_for_application", frozenset({"mcp:read"}))
    assert excinfo.value.required_scope == "mcp:apply"
    assert excinfo.value.tool_name == "queue_job_for_application"


def test_require_tool_scope_empty_set_denies_everything():
    with pytest.raises(InsufficientScopeError):
        require_tool_scope("search_saved_jobs", frozenset())


def test_require_tool_scope_none_is_unrestricted():
    require_tool_scope("queue_job_for_application", None)


def test_require_tool_scope_unknown_tool_unchecked():
    require_tool_scope("not_a_tool", frozenset())


# ---------------------------------------------------------------------------
# Dispatch: scope enforced before validation / handler / DB
# ---------------------------------------------------------------------------


async def test_dispatch_denies_before_db(monkeypatch):
    def _db_touched(*args, **kwargs):
        raise AssertionError("handler/DB must not be reached without the scope")

    monkeypatch.setattr(dispatcher_module, "get_db_connection", _db_touched)
    with pytest.raises(InsufficientScopeError):
        await dispatch_tool_call(
            "queue_job_for_application", {"job_id": "li-1"}, scopes=frozenset({"mcp:read"})
        )


async def test_dispatch_allows_covered_tool(monkeypatch):
    _mock_db(monkeypatch)
    result = await dispatch_tool_call(
        "search_saved_jobs", {}, scopes=frozenset({"mcp:read"})
    )
    assert result == []


async def test_dispatch_no_scopes_is_unrestricted(monkeypatch):
    monkeypatch.setattr(
        dispatcher_module, "_queue_job_for_apply", lambda job_id: {"success": True}
    )
    result = await dispatch_tool_call("queue_job_for_application", {"job_id": "li-1"})
    assert result == {"success": True}


# ---------------------------------------------------------------------------
# HTTP: OAuth token scopes enforced end to end
# ---------------------------------------------------------------------------


def test_read_token_can_read_but_not_apply(client, monkeypatch):
    _mock_db(monkeypatch)
    token = _oauth_token(client, "mcp:read")

    r = _rpc(client, token, "search_saved_jobs", {})
    assert r.status_code == 200
    assert "error" not in r.json()

    r = _rpc(client, token, "queue_job_for_application", {"job_id": "li-1"})
    assert r.status_code == 200
    err = r.json()["error"]
    assert err["code"] == -32001
    assert "mcp:apply" in err["message"]
    assert err["data"]["required_scope"] == "mcp:apply"
    assert err["data"]["tool"] == "queue_job_for_application"


def test_ingest_token_can_stage_but_not_apply(client, monkeypatch):
    monkeypatch.setattr(
        dispatcher_module,
        "stage_agent_jobs_workflow",
        lambda **kwargs: {"status": "success", "staged": []},
    )
    token = _oauth_token(client, "mcp:ingest")

    r = _rpc(
        client,
        token,
        "stage_agent_jobs",
        {"jobs": [{"title": "T", "company": "C", "job_url": "https://example.com/j"}]},
    )
    assert r.status_code == 200
    assert "error" not in r.json()

    r = _rpc(client, token, "run_application_worker", {"limit": 1})
    assert r.json()["error"]["code"] == -32001


def test_legacy_mcp_token_keeps_full_access(client, monkeypatch):
    monkeypatch.setattr(
        dispatcher_module, "_queue_job_for_apply", lambda job_id: {"success": True}
    )
    token = _oauth_token(client, "mcp")
    r = _rpc(client, token, "queue_job_for_application", {"job_id": "li-1"})
    assert r.status_code == 200
    assert "error" not in r.json()


def test_static_bearer_keeps_full_access(client, monkeypatch):
    monkeypatch.setattr(
        dispatcher_module, "_queue_job_for_apply", lambda job_id: {"success": True}
    )
    r = _rpc(client, STATIC_BEARER, "queue_job_for_application", {"job_id": "li-1"})
    assert r.status_code == 200
    assert "error" not in r.json()


def test_scope_error_beats_validation_error(client):
    # A read-scoped caller with garbage args for an apply tool learns
    # nothing about the tool's arguments: scope first, validation second.
    token = _oauth_token(client, "mcp:read")
    r = _rpc(client, token, "queue_job_for_application", {"job_id": 12345})
    assert r.json()["error"]["code"] == -32001


# ---------------------------------------------------------------------------
# Issuance: metadata, registration, authorize
# ---------------------------------------------------------------------------


def test_metadata_lists_all_scopes(client):
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.json()["scopes_supported"] == ["mcp", "mcp:read", "mcp:ingest", "mcp:apply"]
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.json()["scopes_supported"] == ["mcp", "mcp:read", "mcp:ingest", "mcp:apply"]


def test_register_rejects_unknown_scope(client):
    r = client.post(
        "/oauth/register",
        json={"redirect_uris": [REDIRECT_URI], "scope": "mcp:read bogus"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_scope"


def test_authorize_get_rejects_unknown_scope(client):
    creds = _register(client)
    r = client.get(
        "/oauth/authorize",
        params={
            "client_id": creds["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "state": "xyz",
            "scope": "mcp:read bogus",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["error"] == ["invalid_scope"]


def test_authorize_post_rejects_tampered_scope(client):
    creds = _register(client)
    r = client.post(
        "/oauth/authorize",
        data={
            "client_id": creds["client_id"],
            "redirect_uri": REDIRECT_URI,
            "state": "xyz",
            "scope": "bogus",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "owner_token": OWNER_TOKEN,
            "decision": "approve",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Unknown scope" in r.text


def test_approval_page_shows_requested_scopes(client):
    creds = _register(client)
    r = client.get(
        "/oauth/authorize",
        params={
            "client_id": creds["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "state": "xyz",
            "scope": "mcp:read",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 200
    assert "mcp:read" in r.text


def test_token_response_carries_granted_scope(client):
    creds = _register(client)
    code = _approve(client, creds["client_id"], scope="mcp:read mcp:ingest")
    pair = _exchange(client, creds["client_id"], code)
    assert pair["scope"] == "mcp:ingest mcp:read"
