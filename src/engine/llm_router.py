# src/engine/llm_router.py
"""Resilient multi-provider LLM routing with quota-aware failover.

User policy (2026-10-03):
- ANALYSIS (fit score, sponsorship, skill extraction): Groq free -> Gemini free.
  Paid keys are NEVER used here.
- APPLY (browser-use agent): Groq free -> Gemini free -> Gemini paid.
  Free tiers are exhausted first; paid Gemini only engages when free quota is
  gone, so the pipeline keeps running instead of stopping. Paid engagement is
  always logged loudly. Set APPLY_ALLOW_PAID=false to disable the paid step.

Everything is logged: chain construction, per-call provider (DEBUG), every
failover with its reason (WARNING), quota-exhaustion markings (WARNING), and
paid-tier engagement (WARNING). These flow into the service logs, which the
admin API already exposes for remote diagnosis.
"""
import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from src.core.config import settings

logger = logging.getLogger(__name__)

FREE = "free"
PAID = "paid"

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Substrings (lowercased) identifying quota/rate-limit exhaustion.
_QUOTA_KEYWORDS = (
    "429",
    "resource_exhausted",
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
    "daily limit",
    "requests per day",
    "tokens per day",
    "rpd",
    "tpd",
)
# Substrings identifying a billing problem (depleted prepaid balance, etc.).
_BILLING_KEYWORDS = (
    "402",
    "prepayment",
    "billing",
    "credits are depleted",
    "insufficient_quota",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _next_utc_midnight(now: Optional[datetime] = None) -> datetime:
    now = now or _utcnow()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def classify_llm_error(exc: BaseException) -> str:
    """Returns 'quota', 'billing', or 'transient' for an LLM call failure."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(k in text for k in _BILLING_KEYWORDS):
        return "billing"
    if any(k in text for k in _QUOTA_KEYWORDS):
        return "quota"
    return "transient"


# ---------------------------------------------------------------------------
# Chat-model builders (lazy browser-use imports; same pattern as browser_agent)
# ---------------------------------------------------------------------------

def _import_chat_google() -> Any | None:
    try:
        from browser_use.llm import ChatGoogle

        return ChatGoogle
    except ImportError:
        try:
            from browser_use import ChatGoogle  # type: ignore[no-redef]

            return ChatGoogle
        except ImportError:
            return None


def _import_openai_chat() -> Any | None:
    try:
        from browser_use.llm import ChatOpenAI

        return ChatOpenAI
    except ImportError:
        return None


def build_gemini_llm(api_key: str, model: str) -> Any:
    """browser-use ChatGoogle for the given key/model."""
    chat_google = _import_chat_google()
    if chat_google is None:
        raise ImportError(
            "Could not import a Google chat model from browser_use "
            "(tried browser_use.llm.ChatGoogle and browser_use.ChatGoogle)."
        )
    return chat_google(model=model, api_key=api_key)


def build_openai_compat_llm(base_url: str, api_key: str, model: str) -> Any:
    """browser-use ChatOpenAI against any OpenAI-compatible endpoint."""
    chat_openai = _import_openai_chat()
    if chat_openai is None:
        raise ImportError(
            "Could not import browser_use.llm.ChatOpenAI; upgrade browser-use."
        )
    return chat_openai(model=model, api_key=api_key, base_url=base_url)


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------

@dataclass
class Provider:
    """One step in a failover chain. The chat model is built lazily."""

    name: str  # "groq" | "gemini" | "gemini-paid"
    kind: str  # "free" | "paid"
    model: str
    build: Callable[[], Any]
    exhausted_until: Optional[datetime] = None
    consecutive_transient_failures: int = field(default=0, init=False)
    _llm: Any = field(default=None, init=False, repr=False)

    @property
    def llm(self) -> Any:
        if self._llm is None:
            self._llm = self.build()
        return self._llm

    def available(self, now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        return self.exhausted_until is None or now >= self.exhausted_until

    def mark_exhausted(self, until: datetime, reason: str) -> None:
        self.exhausted_until = until
        logger.warning(
            "llm-router: provider=%s marked exhausted until %s (%s)",
            self.name,
            until.isoformat(),
            reason,
        )

    def note_success(self) -> None:
        self.consecutive_transient_failures = 0


class FailoverLLM:
    """Drop-in chat model for browser-use that fails over across providers.

    Implements the attributes the Agent touches (``model``, ``provider``,
    ``name``, ``ainvoke``); everything else delegates to the currently active
    provider's chat model.
    """

    def __init__(self, providers: list[Provider]):
        if not providers:
            raise ValueError("FailoverLLM requires at least one provider.")
        # Set via object.__setattr__ to keep __getattr__ delegation safe.
        object.__setattr__(self, "_providers", providers)
        logger.info(
            "llm-router: failover chain ready: %s",
            " -> ".join(
                f"{p.name}({p.kind}, {p.model})" for p in providers
            ),
        )

    # -- browser-use interface -------------------------------------------
    @property
    def model(self) -> str:
        return self._active().model

    @property
    def provider(self) -> str:
        llm = self._active().llm
        return getattr(llm, "provider", self._active().name)

    @property
    def name(self) -> str:
        return f"failover({self._active().name})"

    def __getattr__(self, item: str) -> Any:
        # Delegates anything else (e.g. model_name) to the active chat model.
        return getattr(object.__getattribute__(self, "_active")().llm, item)

    async def ainvoke(self, messages, output_format=None, **kwargs):
        last_exc: Optional[BaseException] = None
        for provider in self._ordered_available():
            try:
                logger.debug("llm-router: serving call via provider=%s", provider.name)
                result = await provider.llm.ainvoke(
                    messages, output_format=output_format, **kwargs
                )
                provider.note_success()
                return result
            except Exception as exc:  # noqa: BLE001 - must catch provider errors
                last_exc = exc
                kind = classify_llm_error(exc)
                err = str(exc)[:200]
                if kind == "quota":
                    # Free daily quotas reset ~midnight UTC; paid quotas can be
                    # raised, so only a short cooldown there.
                    until = (
                        _next_utc_midnight()
                        if provider.kind == FREE
                        else _utcnow() + timedelta(minutes=30)
                    )
                    provider.mark_exhausted(until, f"quota exhausted: {err}")
                    logger.warning(
                        "llm-router: provider=%s quota exhausted; failing over (%s)",
                        provider.name,
                        err,
                    )
                    continue
                if kind == "billing":
                    provider.mark_exhausted(
                        _utcnow() + timedelta(minutes=30),
                        f"billing problem: {err}",
                    )
                    logger.warning(
                        "llm-router: provider=%s billing problem (depleted credits?); "
                        "failing over (%s)",
                        provider.name,
                        err,
                    )
                    continue
                # Transient: one quick retry, then fail over.
                provider.consecutive_transient_failures += 1
                logger.warning(
                    "llm-router: provider=%s transient error (attempt %d): %s",
                    provider.name,
                    provider.consecutive_transient_failures,
                    err,
                )
                try:
                    await asyncio.sleep(2)
                    result = await provider.llm.ainvoke(
                        messages, output_format=output_format, **kwargs
                    )
                    provider.note_success()
                    logger.info(
                        "llm-router: provider=%s recovered on retry", provider.name
                    )
                    return result
                except Exception as retry_exc:  # noqa: BLE001
                    logger.warning(
                        "llm-router: provider=%s retry failed; failing over (%s)",
                        provider.name,
                        str(retry_exc)[:200],
                    )
                    last_exc = retry_exc
                    continue
        raise RuntimeError(
            f"llm-router: all providers exhausted/failed. Last error: {last_exc}"
        )

    def invoke(self, messages, output_format=None, **kwargs):
        """Synchronous entry point (the Agent uses ainvoke; provided for completeness)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError(
                "llm-router: invoke() called from a running event loop; use ainvoke()."
            )
        return asyncio.run(self.ainvoke(messages, output_format=output_format, **kwargs))

    # -- internals ---------------------------------------------------------
    def _active(self) -> Provider:
        for p in object.__getattribute__(self, "_providers"):
            if p.available():
                return p
        return object.__getattribute__(self, "_providers")[-1]

    def _ordered_available(self) -> list[Provider]:
        now = _utcnow()
        return [p for p in object.__getattribute__(self, "_providers") if p.available(now)]


# ---------------------------------------------------------------------------
# Chain factories
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def get_apply_providers() -> list[Provider]:
    """Builds the APPLY chain: Groq free -> Gemini free -> Gemini paid.

    Providers whose keys are missing are skipped (logged). Paid Gemini is only
    included when APPLY_ALLOW_PAID is not 'false'.
    """
    providers: list[Provider] = []

    groq_key = _env("GROQ_API_KEY", getattr(settings, "GROQ_API_KEY", ""))
    if groq_key:
        groq_model = _env("GROQ_MODEL", getattr(settings, "GROQ_MODEL", "llama-3.3-70b-versatile"))
        providers.append(
            Provider(
                name="groq",
                kind=FREE,
                model=groq_model,
                build=lambda: build_openai_compat_llm(GROQ_BASE_URL, groq_key, groq_model),
            )
        )
    else:
        logger.info("llm-router: GROQ_API_KEY not set; groq skipped in apply chain")

    gemini_key = _env("GEMINI_API_KEY", getattr(settings, "GEMINI_API_KEY", ""))
    if gemini_key:
        gemini_model = _env("GEMINI_MODEL", getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash-lite"))
        providers.append(
            Provider(
                name="gemini",
                kind=FREE,
                model=gemini_model,
                build=lambda: build_gemini_llm(gemini_key, gemini_model),
            )
        )
    else:
        logger.info("llm-router: GEMINI_API_KEY not set; gemini(free) skipped in apply chain")

    allow_paid = _env("APPLY_ALLOW_PAID", "true").lower() != "false"
    paid_key = _env("GEMINI_PAID_API_KEY", getattr(settings, "GEMINI_PAID_API_KEY", ""))
    if paid_key and allow_paid:
        paid_model = _env("GEMINI_PAID_MODEL", getattr(settings, "GEMINI_PAID_MODEL", "gemini-2.5-flash"))
        providers.append(
            Provider(
                name="gemini-paid",
                kind=PAID,
                model=paid_model,
                build=lambda: build_gemini_llm(paid_key, paid_model),
            )
        )
        logger.warning(
            "llm-router: gemini-paid is ARMED in the apply chain — it engages only "
            "after free quotas are exhausted, and usage WILL incur charges."
        )
    elif paid_key and not allow_paid:
        logger.info("llm-router: APPLY_ALLOW_PAID=false; gemini-paid excluded from apply chain")
    else:
        logger.info("llm-router: GEMINI_PAID_API_KEY not set; gemini-paid skipped in apply chain")

    if not providers:
        raise RuntimeError(
            "llm-router: no apply providers configured. Set GROQ_API_KEY and/or "
            "GEMINI_API_KEY and/or GEMINI_PAID_API_KEY."
        )
    return providers


def get_apply_llm() -> FailoverLLM:
    """Failover chat model for the browser-use apply agent."""
    return FailoverLLM(get_apply_providers())
