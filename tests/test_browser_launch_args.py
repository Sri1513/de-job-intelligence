"""build_browser must pass Docker-safe launch args (--no-sandbox etc.)
to the agent's browser in every construction path. Regression test for
the apply-worker Chrome failure: args were only wired into the
session-check paths, never the actual agent run."""
import sys
from unittest.mock import MagicMock, patch

sys.modules.setdefault("browser_use", MagicMock())

from src.engine import browser_agent  # noqa: E402
from src.engine.browser_agent import build_browser, playwright_launch_args  # noqa: E402


def test_launch_args_include_docker_essentials():
    args = playwright_launch_args()
    assert "--no-sandbox" in args
    assert "--disable-dev-shm-usage" in args


def test_build_browser_passes_args_to_browser_init(tmp_path):
    seen = {}

    class FakeBrowser:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    with patch.object(browser_agent, "Browser", FakeBrowser):
        with patch.object(browser_agent, "_browser_init_params",
                          return_value={"headless", "user_data_dir", "args"}):
            build_browser(headless=True, profile_dir=tmp_path)
    assert seen.get("args") == playwright_launch_args()
    assert seen.get("user_data_dir") == str(tmp_path)


def test_build_browser_passes_args_via_config_class(tmp_path):
    seen = {}

    class FakeConfig:
        model_fields = {"headless": 1, "user_data_dir": 1, "args": 1}

        def __init__(self, **kwargs):
            seen.update(kwargs)

    class FakeBrowser:
        def __init__(self, **kwargs):
            seen["browser_kwargs"] = kwargs

    with patch.object(browser_agent, "Browser", FakeBrowser):
        with patch.object(browser_agent, "_browser_init_params", return_value=set()):
            with patch.object(browser_agent, "_config_class_with_field",
                              return_value=FakeConfig):
                build_browser(headless=True, profile_dir=tmp_path)
    assert seen.get("args") == playwright_launch_args()
