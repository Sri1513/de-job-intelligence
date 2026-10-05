# src/engine/llm_text.py
"""Config-driven, quota-aware LLM text generation with automatic failover.

Single entry point: ``generate_text(prompt, ...)`` — used by the analysis,
outreach-email, metadata-extraction, and evaluation paths. The provider chain
lives in ``config/llm.yaml`` (order = failover order); API keys come from the
environment and are never logged or stored in the config file.

Failure handling per provider:
- quota/rate-limit  -> provider sidelined until ~midnight UTC (free daily
                       reset) or +30 min (paid); WhatsApp alert; fail over.
- billing problem    -> provider sidelined 30 min; WhatsApp alert; fail over.
- transient (5xx,    -> one retry after a short delay, then fail over to the
  timeouts, etc.)       next provider; repeated transients sideline briefly.
- all providers down -> WhatsApp alert + RuntimeError (callers keep their own
                       graceful fallbacks, e.g. template email).

This replaces the four hand-rolled Groq/Gemini fallback blocks that used to
live in alert_pipeline.py, email_analyzer.py, analyzer.py, and evaluator.py.
"""
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.engine.llm_router import FREE, PAID, classify_llm_error
from src.engine.notifier import (
    alert_llm_all_down,
    alert_llm_failover,
    alert_paid_engaged,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "llm.yaml"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _next_utc_midnight(now: Optional[datetime] = None) -> datetime:
    now = now or _utcnow()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _resolve_keys(env_names: List[str]) -> List[str]:
    """Returns configured API key values (in order); never logs the values."""
    keys = []
    for name in env_names or []:
        val = (os.getenv(name) or "").strip()
        if val and val not in keys:
            keys.append(val)
    return keys


# ---------------------------------------------------------------------------
# Provider builders — one per serving mechanism (not per vendor)
# ---------------------------------------------------------------------------

def _gemini_generate(api_key: str, model: str, prompt: str, *,
                    json_mode: bool, temperature: float) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json" if json_mode else "text/plain",
            temperature=temperature,
        ),
    )
    return (response.text or "").strip()


def _openai_compat_generate(base_url: str, api_key: str, model: str, prompt: str, *,
                            json_mode: bool, temperature: float) -> str:
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key)
    kwargs: Dict[str, Any] = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        timeout=60.0,
        **kwargs,
    )
    return (response.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Provider model
# ---------------------------------------------------------------------------

@dataclass
class TextProvider:
    """One step in the text-generation failover chain (config-driven)."""

    name: str
    kind: str  # "free" | "paid"
    model: str
    api_keys: List[str] = field(default_factory=list, repr=False)
    base_url: Optional[str] = None
    exhausted_until: Optional[datetime] = None
    transient_cooldown_until: Optional[datetime] = None
    consecutive_transient_failures: int = field(default=0, init=False)

    def available(self, now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        if self.exhausted_until is not None and now < self.exhausted_until:
            return False
        if self.transient_cooldown_until is not None and now < self.transient_cooldown_until:
            return False
        return True

    def mark_exhausted(self, until: datetime, reason: str) -> None:
        self.exhausted_until = until
        logger.warning(
            "llm-text: provider=%s sidelined until %s (%s)",
            self.name, until.isoformat(), reason,
        )

    def note_success(self) -> None:
        self.consecutive_transient_failures = 0
        self.transient_cooldown_until = None

    def generate(self, prompt: str, *, json_mode: bool, temperature: float) -> str:
        """Tries each configured key in order; raises the last error."""
        last_exc: Optional[BaseException] = None
        for idx, key in enumerate(self.api_keys):
            try:
                if self.name == "gemini":
                    return _gemini_generate(
                        key, self.model, prompt,
                        json_mode=json_mode, temperature=temperature,
                    )
                # Any provider with a base_url speaks OpenAI-compatible chat.
                if self.base_url:
                    return _openai_compat_generate(
                        self.base_url, key, self.model, prompt,
                        json_mode=json_mode, temperature=temperature,
                    )
                raise RuntimeError(
                    f"llm-text: no builder for provider '{self.name}' "
                    f"(needs 'base_url' for OpenAI-compatible serving)"
                )
            except Exception as exc:  # noqa: BLE001 - key-level failover
                last_exc = exc
                logger.warning(
                    "llm-text: provider=%s key %d/%d failed: %s",
                    self.name, idx + 1, len(self.api_keys), str(exc)[:200],
                )
        assert last_exc is not None
        raise last_exc


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_text_providers(config_path: Optional[Path] = None) -> List[TextProvider]:
    """Builds the provider chain from config/llm.yaml.

    Providers that are disabled or have no API key configured are skipped
    (logged). Raises RuntimeError when nothing is usable.
    """
    path = Path(config_path) if config_path else Path(
        os.getenv("LLM_CONFIG_PATH", str(DEFAULT_CONFIG_PATH))
    )
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    settings_cfg = cfg.get("settings", {}) or {}

    providers: List[TextProvider] = []
    for entry in cfg.get("providers", []) or []:
        name = entry.get("name", "unnamed")
        if not entry.get("enabled", True):
            logger.info("llm-text: provider=%s disabled in config; skipped", name)
            continue
        keys = _resolve_keys(entry.get("api_key_envs", []))
        if not keys:
            logger.info(
                "llm-text: provider=%s has no API key configured (%s); skipped",
                name, entry.get("api_key_envs"),
            )
            continue
        kind = entry.get("kind", FREE)
        if kind == PAID and not settings_cfg.get("allow_paid", True):
            logger.info("llm-text: provider=%s is paid and allow_paid=false; skipped", name)
            continue
        providers.append(
            TextProvider(
                name=name,
                kind=kind,
                model=entry.get("model", ""),
                api_keys=keys,
                base_url=entry.get("base_url"),
            )
        )
    if not providers:
        raise RuntimeError(
            f"llm-text: no providers usable from {path}. "
            "Set the API key env vars named in config/llm.yaml."
        )
    logger.info(
        "llm-text: chain ready: %s",
        " -> ".join(f"{p.name}({p.kind},{p.model})" for p in providers),
    )
    return providers


# Module-level chain (built lazily so imports never require API keys).
_chain: Optional[List[TextProvider]] = None
_chain_settings: Dict[str, Any] = {}


def _get_chain() -> List[TextProvider]:
    global _chain, _chain_settings
    if _chain is None:
        path = Path(os.getenv("LLM_CONFIG_PATH", str(DEFAULT_CONFIG_PATH)))
        with open(path, "r", encoding="utf-8") as fh:
            _chain_settings = (yaml.safe_load(fh) or {}).get("settings", {}) or {}
        _chain = load_text_providers(path)
    return _chain


def reset_chain() -> None:
    """Clears the cached chain (used by tests)."""
    global _chain, _chain_settings
    _chain = None
    _chain_settings = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_text(
    prompt: str,
    *,
    json_mode: bool = False,
    temperature: float = 0.2,
    task: str = "llm",
    providers: Optional[List[TextProvider]] = None,
) -> str:
    """Generates text via the first available provider, failing over on errors.

    Raises RuntimeError when every provider is exhausted/failed (after a
    WhatsApp alert). Callers should keep graceful fallbacks for that case.
    """
    chain = providers if providers is not None else _get_chain()
    retry_delay = float(_chain_settings.get("transient_retry_delay_s", 2))
    cooldown_s = float(_chain_settings.get("transient_cooldown_s", 120))
    last_exc: Optional[BaseException] = None
    next_provider_name = "none"

    available = [p for p in chain if p.available()]
    for i, provider in enumerate(available):
        next_provider_name = available[i + 1].name if i + 1 < len(available) else "none"
        try:
            result = provider.generate(
                prompt, json_mode=json_mode, temperature=temperature
            )
            provider.note_success()
            if provider.kind == PAID:
                alert_paid_engaged(task, provider.name)
                logger.warning(
                    "llm-text: task=%s now serving via %s (PAID) — usage WILL incur charges",
                    task, provider.name,
                )
            else:
                logger.debug("llm-text: task=%s served via %s", task, provider.name)
            return result
        except Exception as exc:  # noqa: BLE001 - provider-level failover
            last_exc = exc
            kind = classify_llm_error(exc)
            err = str(exc)[:200]
            if kind == "quota":
                until = (
                    _next_utc_midnight()
                    if provider.kind == FREE
                    else _utcnow() + timedelta(minutes=30)
                )
                provider.mark_exhausted(until, f"quota exhausted: {err}")
                alert_llm_failover(task, provider.name, f"quota exhausted ({err})",
                                   next_provider_name)
                continue
            if kind == "billing":
                provider.mark_exhausted(
                    _utcnow() + timedelta(minutes=30), f"billing problem: {err}"
                )
                alert_llm_failover(task, provider.name, f"billing problem ({err})",
                                   next_provider_name)
                continue
            # Transient: one retry, then brief cooldown + fail over.
            provider.consecutive_transient_failures += 1
            logger.warning(
                "llm-text: task=%s provider=%s transient error (attempt %d): %s",
                task, provider.name, provider.consecutive_transient_failures, err,
            )
            try:
                time.sleep(retry_delay)
                result = provider.generate(
                    prompt, json_mode=json_mode, temperature=temperature
                )
                provider.note_success()
                logger.info(
                    "llm-text: task=%s provider=%s recovered on retry", task, provider.name
                )
                return result
            except Exception as retry_exc:  # noqa: BLE001
                last_exc = retry_exc
                provider.transient_cooldown_until = _utcnow() + timedelta(seconds=cooldown_s)
                logger.warning(
                    "llm-text: task=%s provider=%s retry failed; failing over to %s (%s)",
                    task, provider.name, next_provider_name, str(retry_exc)[:200],
                )
                continue

    msg = f"llm-text: all providers exhausted/failed for task={task}. Last error: {last_exc}"
    logger.error(msg)
    alert_llm_all_down(task, str(last_exc)[:300] if last_exc else "unknown")
    raise RuntimeError(msg)
