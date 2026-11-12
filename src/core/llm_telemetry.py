# src/core/llm_telemetry.py
"""Best-effort telemetry for LLM calls -> scout.llm_telemetry_ledger.

One row per LLM invocation (see migrations/007): provider, model, task,
token counts, latency, failover info, estimated cost. Feeds the FinOps
"Cost per Application & Failover Telemetry" widget (Sprint 1).

HARD RULES for this module — telemetry is observability, never the
critical path:
- FAIL-OPEN: ``record_llm_call`` never raises. DB down, table missing
  (a database built before migration 007), bad values — it logs a
  warning and returns None. An LLM call must never fail because its
  telemetry write failed.
- NO PROMPTS, NO SECRETS: only counts, timings, and names are stored.
  Never pass prompt text, responses, or API keys in here.
- KILL SWITCH: ``LLM_TELEMETRY_ENABLED=false`` disables all writes
  (reads the env var on every call so tests can toggle it).
- COST IS AN ESTIMATE (the column is named estimated_cost_usd): free-kind
  providers always cost $0 — free-tier keys are billed nothing, even
  for models that also have a paid rate. Paid-kind providers use the
  published per-1M-token rates below, verified 2026-11-12 against
  Google's Gemini API pricing (2.5 Flash $0.30 in / $2.50 out,
  2.5 Flash-Lite $0.10 / $0.40). A paid model with no rate in the table
  records $0 and logs a warning — never invent a price. Re-check the
  rates when Google changes them; the ledger keeps the estimate that
  was true at write time.
- GRAIN: one row per generate_text / FailoverLLM.ainvoke invocation,
  not per provider attempt. Failed attempts are already in the logs;
  the ledger answers "what did this call cost, how long did it take,
  did it fall over" — so SUM(estimated_cost_usd) is real spend and
  COUNT(*) is calls. ``correlation_id`` / ``tool_name`` stay NULL until
  Sprint 2 threads the MCP request id through (the dispatcher hook).
"""
import asyncio
import logging
import os
from decimal import Decimal
from typing import Optional

from src.core.database import get_pool

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output) for paid-kind providers only.
_PAID_RATES_PER_MILLION = {
    "gemini-2.5-flash": (Decimal("0.30"), Decimal("2.50")),
    "gemini-2.5-flash-lite": (Decimal("0.10"), Decimal("0.40")),
}

_TOKENS_PER_MILLION = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")  # NUMERIC(10,6): 6 decimal places.

_INSERT_SQL = """
    INSERT INTO scout.llm_telemetry_ledger
        (provider, model, task, tool_name, job_id, correlation_id,
         prompt_tokens, completion_tokens, latency_ms,
         fallback_triggered, fallback_reason,
         estimated_cost_usd, status, error_message)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def estimate_cost_usd(
    *, kind: str, model: str, prompt_tokens: int, completion_tokens: int
) -> Decimal:
    """Estimated USD for one call. $0 unless kind == 'paid' with a known rate."""
    if kind != "paid":
        return Decimal("0")
    rates = _PAID_RATES_PER_MILLION.get(model)
    if rates is None:
        logger.warning(
            "llm-telemetry: no paid rate known for model=%s; recording $0",
            model,
        )
        return Decimal("0")
    input_rate, output_rate = rates
    cost = (
        Decimal(max(prompt_tokens, 0)) * input_rate
        + Decimal(max(completion_tokens, 0)) * output_rate
    ) / _TOKENS_PER_MILLION
    return cost.quantize(_COST_QUANTUM)


def _enabled() -> bool:
    return os.getenv("LLM_TELEMETRY_ENABLED", "true").strip().lower() not in (
        "false",
        "0",
        "no",
    )


def record_llm_call(
    *,
    provider: str,
    model: str,
    kind: str = "free",
    task: Optional[str] = None,
    tool_name: Optional[str] = None,
    job_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: Optional[int] = None,
    fallback_triggered: bool = False,
    fallback_reason: Optional[str] = None,
    status: str = "ok",
    error_message: Optional[str] = None,
) -> None:
    """Writes one ledger row. Never raises (see module docstring)."""
    if not _enabled():
        return
    try:
        cost = estimate_cost_usd(
            kind=kind,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        pool = get_pool()
        # Bounded wait: pool.connection() with no timeout waits FOREVER
        # for a connection (the pool default is 30s per call) — a dead
        # database would stall every LLM call instead of failing open.
        # 2s is plenty for a local INSERT and keeps the worst case small.
        with pool.connection(timeout=2) as conn:
            with conn.cursor() as cur:
                # Bound the write: a stuck INSERT must not stall the caller.
                cur.execute("SET LOCAL statement_timeout = '5s'")
                cur.execute(
                    _INSERT_SQL,
                    (
                        provider,
                        model,
                        task,
                        tool_name,
                        job_id,
                        correlation_id,
                        max(prompt_tokens, 0),
                        max(completion_tokens, 0),
                        latency_ms,
                        fallback_triggered,
                        fallback_reason,
                        cost,
                        status,
                        error_message[:500] if error_message else None,
                    ),
                )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break calls
        logger.warning("llm-telemetry: ledger write failed (ignored): %s", exc)


async def arecord_llm_call(**kwargs) -> None:
    """Async wrapper for the apply-agent path (FailoverLLM.ainvoke).

    Runs the blocking psycopg write in a thread so the event loop is
    never stalled by telemetry. Still never raises.
    """
    await asyncio.to_thread(record_llm_call, **kwargs)
