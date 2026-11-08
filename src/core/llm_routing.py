"""Windows-first / VM-fallback routing for the local LLM endpoints.

One shared choke point for the three callers that talk to a local model
over an OpenAI-compatible chat-completions API:

- src/dashboard/portfolio_chat.py  (portfolio Ask agent, chats)
- src/dashboard/market_chat.py     (jobs portal market chat, chats)
- src/engine/local_llm.py          (3B fallback queue, short JSON tasks)

The model split (decided 2026-11-08, Omkar's call): chats request
``qwen2.5:7b`` (fast on his Windows hardware); the queue requests
``settings.LOCAL_LLM_MODEL`` (pinned to ``qwen2.5:3b`` -- the queue's
recall validation was done on the 3B, and the model must not change
without a battery re-run). The router takes the requested model as a
parameter and never swaps it except for the one documented case below.

Resolution, per call:

1. Probe the Windows box (``settings.WINDOWS_LLM_BASE_URL`` +
   ``/api/tags``, ~3s timeout, result cached ~60s). Route to Windows
   ONLY when the probe answers AND lists the requested model. The
   Windows chat-completions URL is ``<base>/v1/chat/completions`` --
   Ollama's OpenAI-compatible route, so the request body the callers
   already send works unchanged.
2. Otherwise use the VM URL (``settings.MARKET_CHAT_LLM_URL``, the
   Open WebUI route) exactly as before. A 7B chat request falling back
   to the VM is downgraded to the VM's model (``settings.LOCAL_LLM_MODEL``,
   ``qwen2.5:3b``) and the downgrade is logged -- a weaker chat answer
   beats an error in a drawer. A queue request is never downgraded:
   it always asks for the VM's model, so the VM retry is the same
   model, seamless.

Auth: the VM key (``settings.MARKET_CHAT_LLM_KEY``) is sent ONLY to the
VM endpoint (Open WebUI needs it). Windows Ollama has no auth -- no key
is ever sent there. The browser-like User-Agent is kept on both paths
(mandatory on the VM path: Cloudflare blocks non-browser signatures
with error 1010; harmless on the Windows path).

Failover: ``attempt_with_failover`` runs the caller's attempt once
against the resolved endpoint; if it raises AND the endpoint was
Windows, it marks Windows down until the next probe window and runs
the attempt once more against the VM endpoint. A VM failure is final
(no second retry). Streaming callers cannot use it directly -- a
generator yields lazily, so a mid-stream failure must not restart the
stream and duplicate tokens -- they retry manually before the first
token only (see market_chat._stream_llm_tokens).

The probe never raises and never blocks a call for long: any probe
error means "Windows unavailable", and the probe cache bounds it to
one short probe per ~60s per process, so a dead Windows box costs
callers nothing after the first probe in a window.

Live-verification limit: this sandbox is NOT on Omkar's Tailscale
network, so the Windows path cannot be curl-verified from here. The
unit tests below cover routing with a mocked probe; the VM shell must
still confirm ``curl http://100.68.109.36:11434/api/tags`` from
job-sscout or llm-prod, plus a chat smoke test through each drawer.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Optional, TypeVar

from src.core.config import settings

logger = logging.getLogger(__name__)

# Cheap liveness probe; the Windows box answers /api/tags in
# milliseconds when it is up, and a 3s cap keeps a dead box from
# stalling a chat request. Cache TTL bounds the probe to ~one per
# minute per process (no probe storm when Windows is down).
PROBE_TIMEOUT_S = 3
PROBE_CACHE_TTL_S = 60

# Same browser-like string class as the dashboard's _post_json and the
# queue's transport: mandatory on the VM path (Cloudflare 1010).
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class LLMEndpoint:
    """One resolved chat-completions target."""

    url: str
    model: str
    auth_headers: Dict[str, str]  # {"Authorization": ...} on VM, {} on Windows
    via: str  # "windows" | "vm"
    downgraded: bool = False  # True when a 7B request fell back to the VM 3B


def _vm_model() -> str:
    """The model the VM's Ollama actually serves (== the queue model)."""
    return settings.LOCAL_LLM_MODEL or "qwen2.5:3b"


def _vm_auth_headers() -> Dict[str, str]:
    key = settings.MARKET_CHAT_LLM_KEY
    return {"Authorization": f"Bearer {key}"} if key else {}


def _windows_base_url() -> str:
    return (settings.WINDOWS_LLM_BASE_URL or "").rstrip("/")


def _windows_tags_url() -> str:
    return _windows_base_url() + "/api/tags"


def _windows_chat_url() -> str:
    return _windows_base_url() + "/v1/chat/completions"


def _vm_endpoint(requested_model: str) -> LLMEndpoint:
    """VM endpoint for a request. Downgrades a non-VM model (today:
    any 7B chat request) to the VM's model and logs it clearly. The
    queue always requests the VM model, so it is never downgraded."""
    requested_model = requested_model or _vm_model()
    model = requested_model
    downgraded = False
    if requested_model != _vm_model():
        model = _vm_model()
        downgraded = True
        logger.warning(
            "LLM routing: model %s is not served by the VM; "
            "falling back to VM model %s",
            requested_model,
            model,
        )
    return LLMEndpoint(
        url=settings.MARKET_CHAT_LLM_URL,
        model=model,
        auth_headers=_vm_auth_headers(),
        via="vm",
        downgraded=downgraded,
    )


def vm_fallback_endpoint(requested_model: str) -> LLMEndpoint:
    """Public VM endpoint for a manual retry (streaming callers, which
    cannot use attempt_with_failover because their attempts are lazy
    generators). Same downgrade rule as _vm_endpoint."""
    return _vm_endpoint(requested_model)


# --- Windows probe (cached) -------------------------------------------------

_probe_lock = threading.Lock()
_probe_models: Optional[FrozenSet[str]] = None
_probe_at: float = 0.0


def _probe_windows() -> FrozenSet[str]:
    """GET /api/tags on the Windows box. Returns the set of model
    names; the empty set on ANY failure (down, unreachable, bad JSON).
    Never raises."""
    base = _windows_base_url()
    if not base:
        return frozenset()
    req = urllib.request.Request(
        _windows_tags_url(),
        headers={"User-Agent": BROWSER_USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        logger.debug("LLM routing: Windows probe failed: %s", exc)
        return frozenset()
    names = {
        (entry.get("name") or "")
        for entry in (data.get("models") or [])
        if isinstance(entry, dict)
    }
    return frozenset(name for name in names if name)


def _windows_models() -> FrozenSet[str]:
    """Cached probe result (TTL PROBE_CACHE_TTL_S)."""
    global _probe_models, _probe_at
    now = time.monotonic()
    with _probe_lock:
        if _probe_models is not None and now - _probe_at < PROBE_CACHE_TTL_S:
            return _probe_models
    models = _probe_windows()
    with _probe_lock:
        _probe_models = models
        _probe_at = now
    return models


def note_windows_failure() -> None:
    """Mark Windows down until the next probe window. Called when a
    Windows attempt fails mid-flight so the retry (and the next calls
    in this window) go straight to the VM."""
    global _probe_models, _probe_at
    with _probe_lock:
        _probe_models = frozenset()
        _probe_at = time.monotonic()


def _reset_probe_cache() -> None:
    """Test-only: clear the probe cache so the next resolve re-probes."""
    global _probe_models, _probe_at
    with _probe_lock:
        _probe_models = None
        _probe_at = 0.0


def resolve_chat_endpoint(requested_model: str) -> LLMEndpoint:
    """Windows-first endpoint for a chat-completions call.

    Windows is used only when the probe lists the requested model;
    anything else (down, unreachable, model absent) resolves to the VM
    endpoint (with the 7B -> VM-3B downgrade logged when it applies).
    Raises RuntimeError when no VM URL is configured at all -- callers
    gate on MARKET_CHAT_LLM_URL before calling, so this is unreachable
    in normal use.
    """
    if not settings.MARKET_CHAT_LLM_URL:
        raise RuntimeError("LLM routing: MARKET_CHAT_LLM_URL is not configured")
    requested_model = requested_model or _vm_model()
    if requested_model in _windows_models():
        return LLMEndpoint(
            url=_windows_chat_url(),
            model=requested_model,
            auth_headers={},
            via="windows",
        )
    return _vm_endpoint(requested_model)


T = TypeVar("T")


def attempt_with_failover(
    requested_model: str, attempt: Callable[[LLMEndpoint], T]
) -> T:
    """Run ``attempt(endpoint)`` once. If it raises AND the endpoint
    was Windows, mark Windows down and run the attempt once more
    against the VM endpoint (downgrading the model if the VM does not
    serve it). The VM attempt's result -- or its exception -- is
    final: no second retry, so a genuinely-down backend fails fast
    instead of looping."""
    endpoint = resolve_chat_endpoint(requested_model)
    try:
        return attempt(endpoint)
    except Exception:
        if endpoint.via != "windows":
            raise
        note_windows_failure()
        logger.warning(
            "LLM routing: Windows endpoint failed for model %s; "
            "retrying once on the VM",
            requested_model,
        )
        return attempt(_vm_endpoint(requested_model))
