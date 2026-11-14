"""Tests for Sprint 2, Batch 6: the bearer-token Ollama proxy.

The proxy fronts the Windows Ollama with the MCP server's bearer
check: only the two paths the LLM router uses are forwarded (GET
/api/tags, POST /v1/chat/completions), and only callers holding the
full "mcp" scope get through -- a read-only OAuth token (mcp:read)
is denied even though it is a valid bearer token.

The upstream Ollama is faked by stubbing urllib.request.urlopen; no
Tailscale, no live Windows box is involved.
"""
import io
import json
import urllib.error
import urllib.request
from http.client import HTTPMessage

import pytest
from starlette.testclient import TestClient

from src.core.config import settings
from src.protocols import token_store as ts_module
from src.protocols.app import create_app
from src.protocols.token_store import get_store

OWNER_TOKEN = "test-owner-token"
WINDOWS_BASE = "http://windows.test:11434"


class _FakeHeaders:
    def __init__(self, content_type="application/json"):
        self._content_type = content_type

    def get_content_type(self):
        return self._content_type


class _FakeUpstream:
    """urlopen stand-in: .status, .headers, .read(n) in chunks, .close()."""

    def __init__(self, payload: bytes, status=200, chunks=None):
        self.status = status
        self.headers = _FakeHeaders()
        self._payload = payload
        self._chunks = chunks

    def read(self, size=-1):
        if self._chunks is not None:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)
        payload, self._payload = self._payload, b""
        return payload if size == -1 else payload[:size]

    def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ts_module, "_store", None)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "MCP_AUTH_TOKEN", OWNER_TOKEN)
    monkeypatch.setattr(settings, "MCP_PUBLIC_URL", "https://mcp.example.com")
    monkeypatch.setattr(settings, "WINDOWS_LLM_BASE_URL", WINDOWS_BASE)
    return TestClient(create_app(auth_token=OWNER_TOKEN))


def _read_only_token():
    return get_store().create_token_pair("test-client", "mcp:read")["access_token"]


def _install_upstream(monkeypatch, payload, status=200, chunks=None):
    seen = []

    def _fake_urlopen(req, timeout=None):
        seen.append((req.full_url, req.get_method(), req.data, timeout))
        if status >= 400:
            headers = HTTPMessage()
            headers.add_header("Content-Type", "application/json")
            raise urllib.error.HTTPError(
                req.full_url, status, "upstream error", headers, io.BytesIO(payload)
            )
        return _FakeUpstream(payload, status=status, chunks=chunks)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return seen


# --- auth -------------------------------------------------------------------


def test_proxy_requires_a_bearer_token(client, monkeypatch):
    _install_upstream(monkeypatch, b'{"models": []}')
    r = client.get("/ollama/api/tags")
    assert r.status_code == 401


def test_proxy_denies_read_only_oauth_token(client, monkeypatch):
    _install_upstream(monkeypatch, b'{"models": []}')
    r = client.get(
        "/ollama/api/tags",
        headers={"Authorization": f"Bearer {_read_only_token()}"},
    )
    assert r.status_code == 403
    assert "full 'mcp' scope" in r.json()["error"]


def test_proxy_allows_full_scope_oauth_token(client, monkeypatch):
    seen = _install_upstream(
        monkeypatch, json.dumps({"models": [{"name": "qwen2.5:7b"}]}).encode()
    )
    token = get_store().create_token_pair("test-client", "mcp")["access_token"]
    r = client.get(
        "/ollama/api/tags", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    assert r.json()["models"][0]["name"] == "qwen2.5:7b"
    assert seen[0][0] == WINDOWS_BASE + "/api/tags"


# --- tags -------------------------------------------------------------------


def test_tags_forwards_to_windows_and_returns_models(client, monkeypatch):
    seen = _install_upstream(
        monkeypatch,
        json.dumps({"models": [{"name": "qwen2.5:7b"}, {"name": "qwen2.5:3b"}]}).encode(),
    )
    r = client.get(
        "/ollama/api/tags", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    )
    assert r.status_code == 200
    assert [m["name"] for m in r.json()["models"]] == ["qwen2.5:7b", "qwen2.5:3b"]
    url, method, body, timeout = seen[0]
    assert url == WINDOWS_BASE + "/api/tags"
    assert method == "GET"
    assert body is None


def test_tags_upstream_down_is_502_without_details(client, monkeypatch):
    def _down(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _down)
    r = client.get(
        "/ollama/api/tags", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    )
    assert r.status_code == 502
    assert r.json() == {"error": "Ollama upstream unavailable"}
    assert WINDOWS_BASE not in r.text


# --- chat -------------------------------------------------------------------


def test_chat_forwards_body_and_streams_chunks(client, monkeypatch):
    chunks = [
        b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    seen = _install_upstream(monkeypatch, b"", chunks=list(chunks))
    r = client.post(
        "/ollama/v1/chat/completions",
        json={"model": "qwen2.5:7b", "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert r.status_code == 200
    assert r.content == b"".join(chunks)
    url, method, body, timeout = seen[0]
    assert url == WINDOWS_BASE + "/v1/chat/completions"
    assert method == "POST"
    assert json.loads(body.decode())["model"] == "qwen2.5:7b"


def test_chat_upstream_error_passes_status_through(client, monkeypatch):
    _install_upstream(
        monkeypatch, b'{"error": "model not found"}', status=404
    )
    r = client.post(
        "/ollama/v1/chat/completions",
        json={"model": "nope", "messages": []},
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert r.status_code == 404
    assert r.json() == {"error": "model not found"}


# --- allowlist ---------------------------------------------------------------


def test_unknown_ollama_path_is_404(client, monkeypatch):
    seen = _install_upstream(monkeypatch, b"{}")
    r = client.get(
        "/ollama/api/generate", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    )
    assert r.status_code == 404
    assert seen == []  # never forwarded upstream


def test_get_on_chat_path_is_not_proxied(client, monkeypatch):
    # GET on the chat path is not an allowlisted (method, path) pair,
    # so the proxy answers 404 and never forwards upstream.
    seen = _install_upstream(monkeypatch, b"{}")
    r = client.get(
        "/ollama/v1/chat/completions",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert r.status_code == 404
    assert seen == []
