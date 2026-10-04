# tests/test_human_pacing.py
"""Unit tests for human-like pacing in the browser agent."""
import asyncio
import inspect
import sys
import time
from unittest.mock import MagicMock

sys.modules.setdefault("browser_use", MagicMock())

from src.engine.browser_agent import (  # noqa: E402  (browser_use stubbed above)
    _make_step_pause_callback,
    _pacing_config,
)


def _clear_env(monkeypatch):
    for var in (
        "HUMAN_PACING",
        "HUMAN_ACTION_DELAY_S",
        "HUMAN_STEP_PAUSE_MIN_S",
        "HUMAN_STEP_PAUSE_MAX_S",
        "HUMAN_MAX_ACTIONS_PER_STEP",
    ):
        monkeypatch.delenv(var, raising=False)


def test_pacing_defaults(monkeypatch):
    _clear_env(monkeypatch)
    cfg = _pacing_config()
    assert cfg is not None
    assert cfg["action_delay"] == 2.0
    assert cfg["pause_min"] == 3.0
    assert cfg["pause_max"] == 7.0
    assert cfg["max_actions_per_step"] == 2


def test_pacing_disabled(monkeypatch):
    _clear_env(monkeypatch)
    for val in ("false", "0", "no", "off"):
        monkeypatch.setenv("HUMAN_PACING", val)
        assert _pacing_config() is None
        monkeypatch.delenv("HUMAN_PACING")


def test_pacing_env_overrides(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HUMAN_ACTION_DELAY_S", "1.5")
    monkeypatch.setenv("HUMAN_STEP_PAUSE_MIN_S", "2")
    monkeypatch.setenv("HUMAN_STEP_PAUSE_MAX_S", "4")
    monkeypatch.setenv("HUMAN_MAX_ACTIONS_PER_STEP", "3")
    cfg = _pacing_config()
    assert cfg["action_delay"] == 1.5
    assert cfg["pause_min"] == 2.0
    assert cfg["pause_max"] == 4.0
    assert cfg["max_actions_per_step"] == 3


def test_pacing_swapped_pause_range_normalized(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HUMAN_STEP_PAUSE_MIN_S", "9")
    monkeypatch.setenv("HUMAN_STEP_PAUSE_MAX_S", "4")
    cfg = _pacing_config()
    assert cfg["pause_min"] == 4.0
    assert cfg["pause_max"] == 9.0


def test_step_pause_callback_sleeps():
    cb = _make_step_pause_callback(0.02, 0.03)
    assert inspect.iscoroutinefunction(cb)
    start = time.monotonic()
    asyncio.run(cb(None, None, 1))
    elapsed = time.monotonic() - start
    assert 0.015 < elapsed < 1.0
