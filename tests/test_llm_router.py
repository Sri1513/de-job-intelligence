# tests/test_llm_router.py
"""Unit tests for the quota-aware LLM failover router."""
import asyncio
import logging
from datetime import timedelta

import pytest

from src.engine import llm_router
from src.engine.llm_router import (
    FREE,
    PAID,
    FailoverLLM,
    Provider,
    classify_llm_error,
    get_apply_providers,
)


class FakeLLM:
    """Minimal stand-in for a browser-use chat model."""

    def __init__(self, model, provider_name, script):
        self.model = model
        self._provider_name = provider_name
        # script: list of "ok" or Exception instances, one per call.
        # When exhausted, the last action repeats.
        self._script = list(script)
        self._last = self._script[-1] if self._script else "ok"
        self.calls = 0

    @property
    def provider(self):
        return self._provider_name

    @property
    def name(self):
        return f"fake-{self.model}"

    async def ainvoke(self, messages, output_format=None, **kwargs):
        self.calls += 1
        action = self._script.pop(0) if self._script else self._last
        if isinstance(action, Exception):
            raise action
        return f"reply-from-{self.model}"


def quota_exc():
    return RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded for the day")


def billing_exc():
    return RuntimeError("402 RESOURCE_EXHAUSTED: Your prepayment credits are depleted")


def make_provider(name, kind, model, script):
    fake = FakeLLM(model, "fake-provider", script)
    return Provider(name=name, kind=kind, model=model, build=lambda: fake), fake


def run(coro):
    return asyncio.run(coro)


# --- error classification -------------------------------------------------

def test_classify_llm_error():
    assert classify_llm_error(quota_exc()) == "quota"
    assert classify_llm_error(RuntimeError("Rate limit exceeded, too many requests")) == "quota"
    assert classify_llm_error(billing_exc()) == "billing"
    assert classify_llm_error(RuntimeError("billing_not_enabled")) == "billing"
    assert classify_llm_error(RuntimeError("500 internal error")) == "transient"
    assert classify_llm_error(ValueError("weird")) == "transient"


# --- failover behavior ----------------------------------------------------

def test_failover_on_quota_marks_free_provider_exhausted(caplog):
    groq, groq_fake = make_provider("groq", FREE, "llama-3.3-70b-versatile", [quota_exc()])
    gemini, gemini_fake = make_provider("gemini", FREE, "gemini-2.5-flash-lite", ["ok"])
    router = FailoverLLM([groq, gemini])

    with caplog.at_level(logging.WARNING, logger="src.engine.llm_router"):
        result = run(router.ainvoke([{"role": "user", "content": "hi"}]))

    assert result == "reply-from-gemini-2.5-flash-lite"
    assert groq_fake.calls == 1
    assert gemini_fake.calls == 1
    # Free provider is parked until ~midnight, so the next call skips it.
    assert not groq.available()
    assert groq.exhausted_until is not None
    result2 = run(router.ainvoke([{"role": "user", "content": "hi again"}]))
    assert result2 == "reply-from-gemini-2.5-flash-lite"
    assert groq_fake.calls == 1  # not tried again
    assert "failing over" in caplog.text
    assert "groq" in caplog.text


def test_paid_provider_engaged_last_and_logged(caplog):
    groq, _ = make_provider("groq", FREE, "llama", [quota_exc()])
    gemini, _ = make_provider("gemini", FREE, "gemini-2.5-flash-lite", [quota_exc()])
    paid, paid_fake = make_provider("gemini-paid", PAID, "gemini-2.5-flash", ["ok"])
    router = FailoverLLM([groq, gemini, paid])

    with caplog.at_level(logging.WARNING, logger="src.engine.llm_router"):
        result = run(router.ainvoke([{"role": "user", "content": "hi"}]))

    assert result == "reply-from-gemini-2.5-flash"
    assert paid_fake.calls == 1
    assert "now serving via gemini-paid (PAID)" in caplog.text
    assert "WILL incur charges" in caplog.text


def test_serving_announcement_not_repeated(caplog):
    groq, groq_fake = make_provider("groq", FREE, "llama", ["ok"])
    router = FailoverLLM([groq])

    with caplog.at_level(logging.INFO, logger="src.engine.llm_router"):
        run(router.ainvoke([{"role": "user", "content": "one"}]))
        run(router.ainvoke([{"role": "user", "content": "two"}]))

    assert groq_fake.calls == 2
    assert caplog.text.count("now serving via groq") == 1


def test_billing_error_gets_short_cooldown_not_midnight():
    _, _ = make_provider("groq", FREE, "llama", ["ok"])
    paid, paid_fake = make_provider("gemini-paid", PAID, "gemini-2.5-flash", [billing_exc(), "ok"])
    router = FailoverLLM([paid])

    with pytest.raises(RuntimeError, match="all providers exhausted"):
        run(router.ainvoke([{"role": "user", "content": "hi"}]))
    assert paid_fake.calls == 1
    # 30-minute cooldown (so a mid-day top-up recovers), not midnight.
    remaining = paid.exhausted_until - llm_router._utcnow()
    assert timedelta(minutes=20) < remaining < timedelta(minutes=31)


def test_transient_error_retries_same_provider_then_fails_over():
    groq, groq_fake = make_provider(
        "groq", FREE, "llama", [RuntimeError("500 boom"), "ok"]
    )
    gemini, gemini_fake = make_provider("gemini", FREE, "gemini-lite", ["ok"])
    router = FailoverLLM([groq, gemini])

    result = run(router.ainvoke([{"role": "user", "content": "hi"}]))
    assert result == "reply-from-llama"
    assert groq_fake.calls == 2  # initial + one retry
    assert gemini_fake.calls == 0  # recovered, no failover
    assert groq.available()  # transient failures don't park the provider


def test_all_providers_fail_raises():
    groq, _ = make_provider("groq", FREE, "llama", [RuntimeError("500")])
    gemini, _ = make_provider("gemini", FREE, "lite", [RuntimeError("503")])
    router = FailoverLLM([groq, gemini])
    with pytest.raises(RuntimeError, match="all providers exhausted"):
        run(router.ainvoke([{"role": "user", "content": "hi"}]))


def test_wrapper_exposes_browser_use_interface():
    groq, _ = make_provider("groq", FREE, "llama-3.3-70b-versatile", ["ok"])
    gemini, _ = make_provider("gemini", FREE, "gemini-2.5-flash-lite", ["ok"])
    router = FailoverLLM([groq, gemini])
    assert router.model == "llama-3.3-70b-versatile"
    assert router.provider == "fake-provider"
    assert "groq" in router.name
    # __getattr__ delegates anything else to the active chat model.
    assert router.calls == 0


def test_empty_chain_rejected():
    with pytest.raises(ValueError):
        FailoverLLM([])


# --- chain construction ---------------------------------------------------

def _write_test_llm_yaml(tmp_path, monkeypatch):
    """Writes a temp config/llm.yaml mirroring the classic test chain."""
    import yaml

    cfg = {
        "providers": [
            {"name": "groq", "kind": "free", "enabled": True,
             "model": "llama-3.3-70b-versatile",
             "base_url": "https://api.groq.com/openai/v1",
             "api_key_envs": ["GROQ_API_KEY"]},
            {"name": "gemini", "kind": "free", "enabled": True,
             "model": "gemini-2.5-flash-lite",
             "api_key_envs": ["GEMINI_API_KEY"]},
            {"name": "gemini-paid", "kind": "paid", "enabled": True,
             "model": "gemini-2.5-flash",
             "api_key_envs": ["GEMINI_PAID_API_KEY"]},
        ],
        "settings": {},
    }
    path = tmp_path / "llm.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("LLM_CONFIG_PATH", str(path))
    return path


def test_get_apply_providers_skips_missing_keys(monkeypatch, tmp_path):
    _write_test_llm_yaml(tmp_path, monkeypatch)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_PAID_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="no providers usable"):
        get_apply_providers()


def test_get_apply_providers_builds_chain_in_order(monkeypatch, tmp_path):
    _write_test_llm_yaml(tmp_path, monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gq-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-test")
    monkeypatch.setenv("GEMINI_PAID_API_KEY", "gm-paid-test")
    providers = get_apply_providers()
    assert [p.name for p in providers] == ["groq", "gemini", "gemini-paid"]
    assert [p.kind for p in providers] == [FREE, FREE, PAID]


def test_apply_allow_paid_false_excludes_paid(monkeypatch, tmp_path):
    _write_test_llm_yaml(tmp_path, monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gq-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-test")
    monkeypatch.setenv("GEMINI_PAID_API_KEY", "gm-paid-test")
    monkeypatch.setenv("APPLY_ALLOW_PAID", "false")
    providers = get_apply_providers()
    assert [p.name for p in providers] == ["groq", "gemini"]


def test_get_apply_providers_expands_multiple_keys(monkeypatch, tmp_path):
    import yaml

    cfg = {
        "providers": [
            {"name": "gemini", "kind": "free", "enabled": True,
             "model": "m", "api_key_envs": ["GEMINI_API_KEY", "GEMINI_API_KEY_2"]},
        ],
        "settings": {},
    }
    path = tmp_path / "llm.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("LLM_CONFIG_PATH", str(path))
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "k2")
    providers = get_apply_providers()
    assert [p.name for p in providers] == ["gemini", "gemini (key 2)"]


# --- evaluator pools ------------------------------------------------------

def test_evaluator_free_and_paid_pools_separated(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "free-key-2")
    monkeypatch.setenv("GEMINI_PAID_API_KEY", "paid-key-1")
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_PAID_API_KEYS", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Avoid picking up a real repo .env during the test.
    monkeypatch.setattr("src.engine.evaluator._load_env_keys", lambda: None)

    from src.engine.evaluator import GeminiKeyRotator

    free = GeminiKeyRotator(paid=False)
    paid = GeminiKeyRotator(paid=True)
    assert free.keys == ["free-key-1", "free-key-2"]
    assert paid.keys == ["paid-key-1"]
    assert not free.paid
    assert paid.paid
