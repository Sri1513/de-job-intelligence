"""SUMMARY_1..6 placeholders filled from multi-line summary; legacy {{SUMMARY}} kept."""
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("browser_use", MagicMock())

from src.synthesis.resume_mapper import (  # noqa: E402  (browser_use stubbed above)
    build_replacement_payload,
)


def _payload(summary):
    return build_replacement_payload(
        {"summary": summary},
        {"slot_headers": {}},
    )


def test_six_summary_lines_fill_placeholders():
    lines = [f"Summary line {i}" for i in range(1, 7)]
    p = _payload("\n".join(lines))
    for i, line in enumerate(lines, start=1):
        assert p[f"{{{{SUMMARY_{i}}}}}"] == line


def test_legacy_summary_placeholder_still_filled():
    p = _payload("Line one\nLine two")
    assert p["{{SUMMARY}}"] == "Line one\nLine two"
    assert p["{{SUMMARY_1}}"] == "Line one"
    assert p["{{SUMMARY_2}}"] == "Line two"
    assert p["{{SUMMARY_3}}"] == ""


def test_extra_lines_truncated_to_six():
    p = _payload("\n".join(f"L{i}" for i in range(8)))
    assert p["{{SUMMARY_6}}"] == "L5"
    assert "{{SUMMARY_7}}" not in p
