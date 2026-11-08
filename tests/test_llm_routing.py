"""Unit tests for the Windows-first / VM-fallback LLM router
(src/core/llm_routing.py) and for the queue transport's use of it.

All network access is faked: the Windows probe and every chat POST go
through a stubbed urllib.request.urlopen. No Tailscale, no live VM,
no live Windows box is involved -- the Windows path cannot be
live-verified from this sandbox (it is not on the tailnet), so these
tests prove the routing decisions, and the VM shell must still confirm
``curl http://100.68.109.36:11434/api/tags`` plus a drawer smoke test.
"""

import json
import time
import urllib.error
import urllib.request

import pytest

from src.core.config import Settings, settings
from src.core import llm_routing
from src.dashboard import market_chat
from src.engine import local_llm

VM_URL = "http://vm.test/api/chat/completions"
WINDOWS_BASE = "http://windows.test:11434"
CHAT_MODEL = "qwen2.5:7b"
QUEUE_MODEL = "qwen2.5:3b"


class _FakeResp:
    """Minimal urlopen stand-in: context manager with .read()."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeStreamResp(_FakeResp):
    """urlopen stand-in that is also iterable over SSE byte lines."""

    def __init__(self, lines):
        super().__init__({})
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    llm_routing._reset_probe_cache()
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", VM_URL)
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_KEY", "vm-key")
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_MODEL", CHAT_MODEL)
    monkeypatch.setattr(settings, "LOCAL_LLM_MODEL", QUEUE_MODEL)
    monkeypatch.setattr(settings, "WINDOWS_LLM_BASE_URL", WINDOWS_BASE)


def _install_probe(monkeypatch, models=("qwen2.5:7b", "qwen2.5:3b"), fail=False):
    """Stub urlopen for the probe only; return the list of probed URLs."""
    probed = []

    def _fake_urlopen(req, timeout=None):
        probed.append(req.full_url)
        if fail:
            raise urllib.error.URLError("windows down")
        return _FakeResp({"models": [{"name": name} for name in models]})

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return probed


# --- resolve ---------------------------------------------------------------

def test_windows_endpoint_when_probe_lists_model(monkeypatch):
    _install_probe(monkeypatch)
    ep = llm_routing.resolve_chat_endpoint(CHAT_MODEL)
    assert ep.via == "windows"
    assert ep.url == WINDOWS_BASE + "/v1/chat/completions"
    assert ep.model == CHAT_MODEL
    assert ep.auth_headers == {}
    assert ep.downgraded is False


def test_vm_endpoint_when_probe_fails(monkeypatch, caplog):
    _install_probe(monkeypatch, fail=True)
    ep = llm_routing.resolve_chat_endpoint(CHAT_MODEL)
    assert ep.via == "vm"
    assert ep.url == VM_URL
    # The VM does not serve the 7B: downgraded to the VM 3B, logged.
    assert ep.model == QUEUE_MODEL
    assert ep.downgraded is True
    assert ep.auth_headers == {"Authorization": "Bearer vm-key"}
    assert "falling back to VM model" in caplog.text


def test_vm_endpoint_when_model_absent_from_windows(monkeypatch):
    _install_probe(monkeypatch, models=("llama3.2:3b",))
    ep = llm_routing.resolve_chat_endpoint(CHAT_MODEL)
    assert ep.via == "vm"
    assert ep.model == QUEUE_MODEL
    assert ep.downgraded is True


def test_queue_model_never_swapped(monkeypatch):
    # Probe down: the queue still gets exactly its model, no downgrade.
    _install_probe(monkeypatch, fail=True)
    ep = llm_routing.resolve_chat_endpoint(QUEUE_MODEL)
    assert ep.via == "vm"
    assert ep.model == QUEUE_MODEL
    assert ep.downgraded is False
    # Probe up: same model on Windows, also no downgrade.
    llm_routing._reset_probe_cache()
    _install_probe(monkeypatch)
    ep = llm_routing.resolve_chat_endpoint(QUEUE_MODEL)
    assert ep.via == "windows"
    assert ep.model == QUEUE_MODEL
    assert ep.downgraded is False


def test_api_key_sent_only_to_vm(monkeypatch):
    _install_probe(monkeypatch)
    assert llm_routing.resolve_chat_endpoint(CHAT_MODEL).auth_headers == {}
    llm_routing._reset_probe_cache()
    _install_probe(monkeypatch, fail=True)
    assert llm_routing.resolve_chat_endpoint(CHAT_MODEL).auth_headers == {
        "Authorization": "Bearer vm-key"
    }


def test_resolve_requires_vm_url(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "")
    with pytest.raises(RuntimeError, match="MARKET_CHAT_LLM_URL"):
        llm_routing.resolve_chat_endpoint(CHAT_MODEL)


# --- probe cache -----------------------------------------------------------

def test_probe_result_cached(monkeypatch):
    probed = _install_probe(monkeypatch)
    llm_routing.resolve_chat_endpoint(CHAT_MODEL)
    llm_routing.resolve_chat_endpoint(CHAT_MODEL)
    llm_routing.resolve_chat_endpoint(QUEUE_MODEL)
    assert len(probed) == 1


def test_probe_cache_expires(monkeypatch):
    probed = _install_probe(monkeypatch)
    llm_routing.resolve_chat_endpoint(CHAT_MODEL)
    llm_routing._probe_at = time.monotonic() - 61
    llm_routing.resolve_chat_endpoint(CHAT_MODEL)
    assert len(probed) == 2


def test_note_windows_failure_marks_down_until_next_window(monkeypatch):
    probed = _install_probe(monkeypatch)
    assert llm_routing.resolve_chat_endpoint(CHAT_MODEL).via == "windows"
    llm_routing.note_windows_failure()
    assert llm_routing.resolve_chat_endpoint(CHAT_MODEL).via == "vm"
    assert len(probed) == 1  # no re-probe inside the window
    llm_routing._probe_at = time.monotonic() - 61
    assert llm_routing.resolve_chat_endpoint(CHAT_MODEL).via == "windows"
    assert len(probed) == 2


# --- attempt_with_failover -------------------------------------------------

def test_attempt_windows_success_calls_once(monkeypatch):
    _install_probe(monkeypatch)
    seen = []
    result = llm_routing.attempt_with_failover(
        CHAT_MODEL, lambda ep: seen.append(ep) or "ok"
    )
    assert result == "ok"
    assert len(seen) == 1
    assert seen[0].via == "windows"


def test_attempt_windows_failure_retries_once_on_vm(monkeypatch, caplog):
    _install_probe(monkeypatch)
    seen = []

    def _attempt(ep):
        seen.append(ep)
        if ep.via == "windows":
            raise urllib.error.URLError("windows hung up")
        return "vm-ok"

    result = llm_routing.attempt_with_failover(CHAT_MODEL, _attempt)
    assert result == "vm-ok"
    assert len(seen) == 2
    assert seen[0].via == "windows"
    assert seen[1].via == "vm"
    # The retry is the downgraded VM model, and it was logged.
    assert seen[1].model == QUEUE_MODEL
    assert seen[1].downgraded is True
    assert "retrying once on the VM" in caplog.text
    # Windows is now marked down for the rest of the window.
    assert llm_routing.resolve_chat_endpoint(CHAT_MODEL).via == "vm"


def test_attempt_vm_failure_raises_without_retry(monkeypatch):
    _install_probe(monkeypatch, fail=True)
    seen = []

    def _attempt(ep):
        seen.append(ep)
        raise RuntimeError("vm also down")

    with pytest.raises(RuntimeError, match="vm also down"):
        llm_routing.attempt_with_failover(CHAT_MODEL, _attempt)
    assert len(seen) == 1


def test_attempt_queue_model_retry_keeps_model(monkeypatch):
    _install_probe(monkeypatch)
    seen = []

    def _attempt(ep):
        seen.append(ep)
        if ep.via == "windows":
            raise urllib.error.URLError("windows hung up")
        return "vm-ok"

    assert llm_routing.attempt_with_failover(QUEUE_MODEL, _attempt) == "vm-ok"
    assert [ep.model for ep in seen] == [QUEUE_MODEL, QUEUE_MODEL]
    assert all(ep.downgraded is False for ep in seen)


# --- config defaults ---------------------------------------------------------

def test_config_defaults_pin_windows_and_queue_model():
    fields = Settings.model_fields
    assert fields["WINDOWS_LLM_BASE_URL"].default == "http://100.68.109.36:11434"
    assert fields["LOCAL_LLM_MODEL"].default == "qwen2.5:3b"
    assert fields["MARKET_CHAT_LLM_MODEL"].default == "qwen2.5:3b"


# --- queue transport through the router -------------------------------------

def test_queue_chat_complete_retries_windows_then_vm(monkeypatch):
    requests = []

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        requests.append((url, req.get_header("Authorization"), body.get("model")))
        if url.endswith("/api/tags"):
            return _FakeResp({"models": [{"name": QUEUE_MODEL}]})
        if "windows.test" in url:
            raise urllib.error.URLError("windows down")
        return _FakeResp({"choices": [{"message": {"content": "hello"}}]})

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert local_llm._chat_complete("Say hi.", timeout=5) == "hello"
    assert requests[0][0].endswith("/api/tags")
    assert "windows.test" in requests[1][0]
    assert requests[1][1] is None  # no key sent to Windows
    assert requests[1][2] == QUEUE_MODEL
    assert "vm.test" in requests[2][0]
    assert requests[2][1] == "Bearer vm-key"  # key sent to the VM only
    # The VM retry asked for the queue model -- never a swap.
    assert requests[2][2] == QUEUE_MODEL


def test_queue_vm_attempt_without_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_KEY", "")

    def _fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/api/tags"):
            return _FakeResp({"models": [{"name": QUEUE_MODEL}]})
        raise urllib.error.URLError("windows down")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(
        local_llm.LocalLLMUnavailableError, match="MARKET_CHAT_LLM_KEY is empty"
    ):
        local_llm._chat_complete("Say hi.", timeout=5)


# --- streaming retry (market_chat) ------------------------------------------

def _sse_lines(*texts):
    lines = [
        ('data: {"choices": [{"delta": {"content": "%s"}}]}' % text).encode()
        for text in texts
    ]
    return lines + [b"data: [DONE]"]


def test_stream_retries_before_first_token(monkeypatch):
    seen = []

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        seen.append((url, body.get("model")))
        if url.endswith("/api/tags"):
            return _FakeResp({"models": [{"name": CHAT_MODEL}]})
        if "windows.test" in url:
            raise urllib.error.URLError("windows hung up")
        return _FakeStreamResp(_sse_lines("hi"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert list(market_chat._stream_llm_tokens([{"role": "user", "content": "x"}])) == [
        "hi"
    ]
    # Probe, failed Windows stream, retried VM stream (downgraded 7B -> 3B).
    assert seen[0][0].endswith("/api/tags")
    assert "windows.test" in seen[1][0] and seen[1][1] == CHAT_MODEL
    assert "vm.test" in seen[2][0] and seen[2][1] == QUEUE_MODEL


def test_stream_does_not_restart_after_first_token(monkeypatch):
    calls = []

    # A response whose iterator raises after yielding one token.
    class _FailingStreamResp(_FakeStreamResp):
        def __iter__(self):
            yield _sse_lines("one")[0]
            raise urllib.error.URLError("windows died mid-stream")

    def _fake_urlopen_failing(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if url.endswith("/api/tags"):
            return _FakeResp({"models": [{"name": CHAT_MODEL}]})
        return _FailingStreamResp([])

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_failing)
    with pytest.raises(urllib.error.URLError):
        list(market_chat._stream_llm_tokens([{"role": "user", "content": "x"}]))
    # Probe + one Windows stream attempt; no VM retry after a token.
    assert len(calls) == 2
    assert all("vm.test" not in url for url in calls)
