# tests/test_mcp_oauth.py
"""End-to-end tests for the OAuth 2.1 authorization server (src/protocols/oauth.py).

Covers discovery metadata, dynamic client registration, the owner-approval
authorize flow, PKCE code exchange, RPC access with OAuth tokens, and refresh
rotation. Uses an isolated token store rooted at tmp_path.
"""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.protocols import token_store as ts_module
from src.protocols.app import create_app

OWNER_TOKEN = "owner-secret-token"

# RFC 7636 Appendix B test vector
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()

REDIRECT_URI = "https://gemini.example.com/oauth/callback"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ts_module, "_store", None)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", OWNER_TOKEN)
    monkeypatch.setattr(settings, "MCP_PUBLIC_URL", "https://mcp.example.com")
    return TestClient(create_app(auth_token="test-bearer"))


def _register(client) -> dict:
    response = client.post(
        "/oauth/register",
        json={"redirect_uris": [REDIRECT_URI], "client_name": "Test Client"},
    )
    assert response.status_code == 201
    return response.json()


def _approve(client, client_id: str, owner_token: str = OWNER_TOKEN) -> str:
    """Runs the authorize POST approval; returns the issued code."""
    response = client.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": "xyz",
            "scope": "mcp",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "owner_token": owner_token,
            "decision": "approve",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query["code"][0]


def _exchange(client, client_id: str, code: str, verifier: str = VERIFIER) -> dict:
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    return response


# -- discovery ---------------------------------------------------------------


def test_authorization_server_metadata(client):
    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    data = response.json()
    assert data["issuer"] == "https://mcp.example.com"
    assert data["token_endpoint"].endswith("/oauth/token")
    assert "S256" in data["code_challenge_methods_supported"]


def test_protected_resource_metadata(client):
    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    data = response.json()
    assert data["authorization_servers"] == ["https://mcp.example.com"]


# -- registration -------------------------------------------------------------


def test_register_rejects_bad_redirect_uri(client):
    for bad in ["not-a-uri", "javascript:alert(1)", "ftp://files.example/x", "http://evil.example/cb"]:
        response = client.post("/oauth/register", json={"redirect_uris": [bad]})
        assert response.status_code == 400, f"accepted {bad}"


def test_register_rejects_empty_redirect_uris(client):
    response = client.post("/oauth/register", json={"redirect_uris": []})
    assert response.status_code == 400


# -- authorize -----------------------------------------------------------------


def test_authorize_get_renders_approval_page(client):
    creds = _register(client)
    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": creds["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
    )
    assert response.status_code == 200
    assert "Authorize" in response.text


def test_authorize_get_rejects_unregistered_redirect(client):
    creds = _register(client)
    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": creds["client_id"],
            "redirect_uri": "https://evil.example/callback",
            "code_challenge": CHALLENGE,
        },
    )
    assert response.status_code == 400


def test_authorize_post_wrong_owner_token(client):
    creds = _register(client)
    response = client.post(
        "/oauth/authorize",
        data={
            "client_id": creds["client_id"],
            "redirect_uri": REDIRECT_URI,
            "state": "xyz",
            "scope": "mcp",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "owner_token": "wrong-token",
            "decision": "approve",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_authorize_post_deny_redirects_with_error(client):
    creds = _register(client)
    response = client.post(
        "/oauth/authorize",
        data={
            "client_id": creds["client_id"],
            "redirect_uri": REDIRECT_URI,
            "state": "xyz",
            "scope": "mcp",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "owner_token": OWNER_TOKEN,
            "decision": "deny",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["error"] == ["access_denied"]


# -- token exchange --------------------------------------------------------------


def test_pkce_exchange_happy_path(client):
    creds = _register(client)
    code = _approve(client, creds["client_id"])
    response = _exchange(client, creds["client_id"], code)
    assert response.status_code == 200
    data = response.json()
    assert data["token_type"] == "Bearer"
    assert data["access_token"]
    assert data["refresh_token"]


def test_pkce_wrong_verifier_rejected(client):
    creds = _register(client)
    code = _approve(client, creds["client_id"])
    response = _exchange(client, creds["client_id"], code, verifier="wrong-verifier")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_code_single_use(client):
    creds = _register(client)
    code = _approve(client, creds["client_id"])
    assert _exchange(client, creds["client_id"], code).status_code == 200
    second = _exchange(client, creds["client_id"], code)
    assert second.status_code == 400


# -- using the tokens -------------------------------------------------------------


def test_rpc_with_oauth_token(client):
    creds = _register(client)
    code = _approve(client, creds["client_id"])
    tokens = _exchange(client, creds["client_id"], code).json()
    response = client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 200


def test_rpc_static_bearer_still_works(client):
    response = client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Bearer test-bearer"},
    )
    assert response.status_code == 200


def test_401_carries_www_authenticate_discovery(client):
    response = client.post("/rpc", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
    www_auth = response.headers["www-authenticate"]
    assert "oauth-protected-resource" in www_auth


def test_refresh_token_rotation(client):
    creds = _register(client)
    code = _approve(client, creds["client_id"])
    tokens = _exchange(client, creds["client_id"], code).json()

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": creds["client_id"],
        },
    )
    assert response.status_code == 200
    new_tokens = response.json()
    assert new_tokens["access_token"] != tokens["access_token"]

    # Old refresh token is single-use.
    replay = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": creds["client_id"],
        },
    )
    assert replay.status_code == 400
