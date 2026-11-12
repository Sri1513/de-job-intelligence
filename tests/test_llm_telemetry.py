# tests/test_llm_telemetry.py
"""Unit tests for the LLM telemetry ledger writer + hooks (Sprint 1, Batch 2).

No database is needed: the pool is faked, and the generate_text /
FailoverLLM hooks are tested by monkeypatching the recorder.
"""
import asyncio
from decimal import Decimal

import pytest

from src.core import llm_telemetry
from src.core.llm_telemetry import estimate_cost_usd, record_llm_call
from src.engine import llm_router, llm_text
from src.engine.llm_router import FREE, FailoverLLM, Provider
from src.engine.llm_text import GenerationResult, TextProvider, generate_text

# --- fake pool -------------------------------------------------------------

class FakeCursor:
    def __init__(self):
        self.statements = []  # (sql, params) in order

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePool:
    """Stands in for psycopg_pool.ConnectionPool."""

    def __init__(self, cursor):
        self._cursor = cursor

    def connection(self, timeout=None):
        return FakeConnection(self._cursor)


class RaisingPool:
    """Simulates a dead database: every use raises."""

    def connection(self, timeout=None):
        raise RuntimeError("could not connect to server")


@pytest.fixture
def fake_pool(monkeypatch):
    cursor = FakeCursor()
    pool = FakePool(cursor)
    monkeypatch.setattr(llm_telemetry, "get_pool", lambda: pool)
    return cursor


# --- cost math -------------------------------------------------------------

def test_estimate_cost_free_kind_is_always_zero():
    # Even for a model that HAS a paid rate: free-tier keys are billed $0.
    assert estimate_cost_usd(
        kind="free", model="gemini-2.5-flash",
        prompt_tokens=100_000, completion_tokens=50_000,
    ) == Decimal("0")


def test_estimate_cost_paid_flash():
    # 1000 in * $0.30/1M + 500 out * $2.50/1M = 0.0003 + 0.00125.
    assert estimate_cost_usd(
        kind="paid", model="gemini-2.5-flash",
        prompt_tokens=1000, completion_tokens=500,
    ) == Decimal("0.001550")


def test_estimate_cost_paid_flash_lite():
    assert estimate_cost_usd(
        kind="paid", model="gemini-2.5-flash-lite",
        prompt_tokens=1000, completion_tokens=1000,
    ) == Decimal("0.000500")


def test_estimate_cost_unknown_paid_model_is_zero():
    # Never invent a price for a model with no published rate.
    assert estimate_cost_usd(
        kind="paid", model="gemini-9.9-ultra",
        prompt_tokens=1000, completion_tokens=1000,
    ) == Decimal("0")


# --- record_llm_call --------------------------------------------------------

def test_record_inserts_one_row_with_expected_params(fake_pool):
    record_llm_call(
        provider="groq", model="openai/gpt-oss-120b", kind="free",
        task="fit-score", prompt_tokens=120, completion_tokens=30,
        latency_ms=850, fallback_triggered=True, fallback_reason="quota",
    )
    assert len(fake_pool.statements) == 2  # SET LOCAL + INSERT
    assert "statement_timeout" in fake_pool.statements[0][0]
    sql, params = fake_pool.statements[1]
    assert "INSERT INTO scout.llm_telemetry_ledger" in sql
    assert params[:8] == (
        "groq", "openai/gpt-oss-120b", "fit-score", None, None, None, 120, 30,
    )
    assert params[8] == 850  # latency_ms
    assert params[9] is True and params[10] == "quota"
    assert params[11] == Decimal("0")  # free kind -> $0
    assert params[12] == "ok"


def test_record_fail_open_when_db_is_down(monkeypatch):
    monkeypatch.setattr(llm_telemetry, "get_pool", lambda: RaisingPool())
    # Must not raise: telemetry never breaks the caller.
    record_llm_call(provider="gemini", model="m", prompt_tokens=1)


def test_record_disabled_by_env(monkeypatch, fake_pool):
    monkeypatch.setenv("LLM_TELEMETRY_ENABLED", "false")
    record_llm_call(provider="gemini", model="m", prompt_tokens=1)
    assert fake_pool.statements == []  # pool never touched


# --- generate_text hook ----------------------------------------------------

class ScriptedProvider(TextProvider):
    """TextProvider returning scripted GenerationResults."""

    def __init__(self, name, script, **kwargs):
        super().__init__(name=name, kind="free", model="test-model",
                         api_keys=["k"], **kwargs)
        self._script = list(script)

    def generate(self, prompt, *, json_mode, temperature):
        action = self._script.pop(0) if self._script else self._script[-1]
        if isinstance(action, Exception):
            raise action
        return action


def test_generate_text_records_row_with_tokens(monkeypatch):
    rows = []
    monkeypatch.setattr(llm_text, "record_llm_call",
                        lambda **kw: rows.append(kw))
    monkeypatch.setattr(llm_text, "alert_llm_failover", lambda *a: None)
    p = ScriptedProvider("groq", [GenerationResult("hi", 120, 30)])
    out = generate_text("hi", task="fit-score", providers=[p])
    assert out == "hi"
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "groq" and row["task"] == "fit-score"
    assert row["prompt_tokens"] == 120 and row["completion_tokens"] == 30
    assert row["fallback_triggered"] is False and row["status"] == "ok"
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0


def test_generate_text_records_fallback_after_quota(monkeypatch):
    rows = []
    monkeypatch.setattr(llm_text, "record_llm_call",
                        lambda **kw: rows.append(kw))
    monkeypatch.setattr(llm_text, "alert_llm_failover", lambda *a: None)
    p1 = ScriptedProvider("gemini", [RuntimeError("429 quota exceeded")])
    p2 = ScriptedProvider("groq", [GenerationResult("ok", 10, 5)])
    out = generate_text("hi", task="t", providers=[p1, p2])
    assert out == "ok"
    assert len(rows) == 1  # one row per invocation, not per attempt
    assert rows[0]["provider"] == "groq"
    assert rows[0]["fallback_triggered"] is True
    assert rows[0]["fallback_reason"] == "quota"


def test_generate_text_records_error_row_when_all_down(monkeypatch):
    rows = []
    monkeypatch.setattr(llm_text, "record_llm_call",
                        lambda **kw: rows.append(kw))
    monkeypatch.setattr(llm_text, "alert_llm_failover", lambda *a: None)
    monkeypatch.setattr(llm_text, "alert_llm_all_down", lambda *a: None)
    p1 = ScriptedProvider("gemini", [RuntimeError("429 quota exceeded")])
    with pytest.raises(RuntimeError, match="all providers exhausted"):
        generate_text("hi", task="t", providers=[p1])
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["provider"] == "gemini"
    assert "429" in rows[0]["error_message"]


# --- FailoverLLM hook ------------------------------------------------------

class FakeChatResult:
    """Stand-in for a LangChain AIMessage carrying usage_metadata."""

    def __init__(self, usage):
        self.usage_metadata = usage


class FakeChatModel:
    def __init__(self, script):
        self._script = list(script)

    async def ainvoke(self, messages, output_format=None, **kwargs):
        action = self._script.pop(0) if self._script else self._script[-1]
        if isinstance(action, Exception):
            raise action
        return FakeChatResult({"input_tokens": 200, "output_tokens": 40})


def test_ainvoke_records_row_with_usage(monkeypatch):
    rows = []

    async def _recorder(**kw):
        rows.append(kw)

    monkeypatch.setattr(llm_router, "arecord_llm_call", _recorder)

    model = FakeChatModel(["ok"])
    provider = Provider(name="gemini", kind=FREE, model="gemini-2.5-flash-lite",
                        build=lambda: model)
    router = FailoverLLM([provider])
    asyncio.run(router.ainvoke([{"role": "user", "content": "hi"}]))
    assert len(rows) == 1
    assert rows[0]["provider"] == "gemini" and rows[0]["task"] == "apply-agent"
    assert rows[0]["prompt_tokens"] == 200 and rows[0]["completion_tokens"] == 40
    assert rows[0]["fallback_triggered"] is False
