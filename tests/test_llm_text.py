# tests/test_llm_text.py
"""Unit tests for the config-driven LLM text failover (llm_text)."""
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from src.engine import llm_text
from src.engine.llm_text import (
    TextProvider,
    generate_text,
    load_text_providers,
    reset_chain,
)


def quota_exc():
    return RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded for the day")


def transient_exc():
    return RuntimeError("503 UNAVAILABLE: model experiencing high demand")


class ScriptedProvider(TextProvider):
    """TextProvider whose generate() follows a script of ok/Exception."""

    def __init__(self, name, script, **kwargs):
        super().__init__(name=name, kind="free", model="test-model",
                         api_keys=["k"], **kwargs)
        self._script = list(script)
        self.calls = 0

    def generate(self, prompt, *, json_mode, temperature):
        self.calls += 1
        action = self._script.pop(0) if self._script else self._script[-1]
        if isinstance(action, Exception):
            raise action
        return action


@pytest.fixture(autouse=True)
def _no_chain_cache():
    reset_chain()
    yield
    reset_chain()


def test_failover_on_quota_marks_provider_exhausted(monkeypatch):
    alerts = []
    monkeypatch.setattr(llm_text, "alert_llm_failover",
                        lambda *a: alerts.append(a))
    p1 = ScriptedProvider("gemini", [quota_exc()])
    p2 = ScriptedProvider("groq", ["hello from groq"])
    out = generate_text("hi", task="t", providers=[p1, p2])
    assert out == "hello from groq"
    assert p1.calls == 1 and p2.calls == 1
    assert not p1.available()  # sidelined until ~midnight UTC
    assert alerts and alerts[0][1] == "gemini"


def test_transient_error_retries_then_fails_over(monkeypatch):
    monkeypatch.setattr(llm_text.time, "sleep", lambda s: None)
    p1 = ScriptedProvider("gemini", [transient_exc(), transient_exc()])
    p2 = ScriptedProvider("groq", ["recovered via groq"])
    out = generate_text("hi", task="t", providers=[p1, p2])
    assert out == "recovered via groq"
    assert p1.calls == 2  # initial + one retry


def test_transient_recovery_on_retry(monkeypatch):
    monkeypatch.setattr(llm_text.time, "sleep", lambda s: None)
    p1 = ScriptedProvider("gemini", [transient_exc(), "recovered"])
    out = generate_text("hi", task="t", providers=[p1])
    assert out == "recovered"
    assert p1.available()  # success clears the transient state


def test_all_providers_down_raises_and_alerts(monkeypatch):
    all_down = []
    monkeypatch.setattr(llm_text, "alert_llm_all_down",
                        lambda *a: all_down.append(a))
    monkeypatch.setattr(llm_text.time, "sleep", lambda s: None)
    p1 = ScriptedProvider("gemini", [quota_exc()])
    p2 = ScriptedProvider("groq", [quota_exc()])
    with pytest.raises(RuntimeError, match="all providers exhausted"):
        generate_text("hi", task="t", providers=[p1, p2])
    assert all_down and all_down[0][0] == "t"


def test_key_rotation_within_provider():
    seen_keys = []

    class KeyRotating(TextProvider):
        def generate(self, prompt, *, json_mode, temperature):
            last = None
            for k in self.api_keys:
                seen_keys.append(k)
                if k == "bad":
                    last = RuntimeError("401 invalid key")
                    continue
                return f"ok-with-{k}"
            raise last

    p = KeyRotating(name="gemini", kind="free", model="m",
                    api_keys=["bad", "good"])
    out = generate_text("hi", task="t", providers=[p])
    assert out == "ok-with-good"
    assert seen_keys == ["bad", "good"]


def test_load_providers_respects_config(tmp_path, monkeypatch):
    cfg = {
        "providers": [
            {"name": "gemini", "kind": "free", "enabled": True,
             "model": "m1", "api_key_envs": ["TEST_KEY_A"]},
            {"name": "groq", "kind": "free", "enabled": False,
             "model": "m2", "base_url": "https://x",
             "api_key_envs": ["TEST_KEY_B"]},
            {"name": "nokey", "kind": "free", "enabled": True,
             "model": "m3", "api_key_envs": ["TEST_KEY_MISSING"]},
        ],
        "settings": {},
    }
    path = tmp_path / "llm.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("TEST_KEY_A", "secret-a")
    monkeypatch.setenv("TEST_KEY_B", "secret-b")
    monkeypatch.delenv("TEST_KEY_MISSING", raising=False)

    providers = load_text_providers(path)
    assert [p.name for p in providers] == ["gemini"]  # disabled + keyless skipped
    assert providers[0].api_keys == ["secret-a"]


def test_load_providers_empty_chain_raises(tmp_path):
    path = tmp_path / "llm.yaml"
    path.write_text(yaml.safe_dump({"providers": [], "settings": {}}))
    with pytest.raises(RuntimeError, match="no providers usable"):
        load_text_providers(path)


def test_repo_config_loads_with_fake_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    providers = load_text_providers(Path("config/llm.yaml"))
    assert [p.name for p in providers] == ["gemini", "groq"]


def test_paid_excluded_by_default_included_when_opted_in(tmp_path, monkeypatch):
    cfg = {
        "providers": [
            {"name": "gemini", "kind": "free", "enabled": True,
             "model": "m1", "api_key_envs": ["TEST_KEY_A"]},
            {"name": "gemini-paid", "kind": "paid", "enabled": True,
             "model": "m2", "api_key_envs": ["TEST_KEY_P"]},
        ],
        "settings": {},
    }
    path = tmp_path / "llm.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("TEST_KEY_A", "a")
    monkeypatch.setenv("TEST_KEY_P", "p")

    free_only = load_text_providers(path)
    assert [p.name for p in free_only] == ["gemini"]
    with_opt_in = load_text_providers(path, allow_paid=True)
    assert [p.name for p in with_opt_in] == ["gemini", "gemini-paid"]
