# tests/test_company_context.py — Sprint 3, Lesson 3.
# The company-context selector + acronym glossary, and their wiring
# into build_job_tailoring_prompt. The WhatsApp path (job_override) is
# used for the builder tests: no DB connection, so no fakes needed.
from unittest.mock import patch

import pytest

from src.engine.company_context import (
    build_acronym_rule,
    select_company_context,
)
from src.synthesis.prompt_builder import build_job_tailoring_prompt

SLOTS = {
    "job1": "herc_rentals",
    "job2": "blue_yonder",
    "job3": "accenture",
    "job4": "thomson_reuters",
}

INLINE_DATA = {
    "companies": {
        "herc_rentals": {
            "company_name": "Herc Rentals Inc.",
            "domain": "Equipment Rental",
            "business_context": "Legacy full-refresh loads were slow.",
            "tech_stack": ["Python", "Databricks"],
            "metrics_and_impact": {"runtime_reduction": "Cut batch by 25%. "},
            "process_framework": {"phase_1_ingestion": {"phase_name": "Ingestion"}},
        }
    }
}


def test_select_shapes_entry_without_phases():
    """Shaped entry keeps the five prompt fields; the phases stay out."""
    ctx = select_company_context({"job1": "herc_rentals"}, INLINE_DATA)
    entry = ctx["job1"]
    assert entry["company_name"] == "Herc Rentals Inc."
    assert entry["domain"] == "Equipment Rental"
    assert entry["business_context"] == "Legacy full-refresh loads were slow."
    assert entry["tech_stack"] == ["Python", "Databricks"]
    assert entry["metrics_and_impact"] == {"runtime_reduction": "Cut batch by 25%. "}
    assert "process_framework" not in entry


def test_select_missing_company_is_none():
    """A slot with no company entry maps to None — never an invented entry."""
    ctx = select_company_context({"job1": "optum", "job2": "blue_yonder"}, INLINE_DATA)
    assert ctx["job1"] is None
    assert ctx["job2"] is None  # blue_yonder not in INLINE_DATA either


def test_select_empty_data_is_all_none():
    for data in ({}, {"companies": {}}, {"companies": None}):
        ctx = select_company_context(SLOTS, data)
        assert ctx == {slot: None for slot in SLOTS}


def test_select_loads_real_file_when_data_omitted():
    """No data passed: the real company_frameworks.json is loaded."""
    ctx = select_company_context(SLOTS)
    assert ctx["job1"]["company_name"] == "Herc Rentals Inc."
    assert ctx["job2"]["company_name"] == "Blue Yonder"
    assert ctx["job3"]["company_name"] != ""
    assert ctx["job4"]["metrics_and_impact"] != {}


def test_select_real_file_has_no_optum():
    ctx = select_company_context({"job1": "optum"})
    assert ctx["job1"] is None


def test_acronym_rule_names_mcp():
    rule = build_acronym_rule()
    assert rule.startswith("ACRONYM RULE")
    assert "MCP = Model Context Protocol" in rule
    assert "NEVER expand as Microsoft Certified Professional" in rule


def test_acronym_rule_empty_glossary_is_empty():
    assert build_acronym_rule({}) == ""


def _whatsapp_bundle(slot_frameworks):
    override = {
        "job_id": "wa-l3",
        "title": "Data Engineer",
        "company": "Ask Consulting",
        "location": "United States",
        "is_remote": True,
        "job_url": "",
        "description": "Healthcare data engineer, PHI, PySpark, AWS.",
        "job_category": "data_engineering",
        "metadata": {"source": "whatsapp"},
        "notes": "",
    }
    with patch("src.synthesis.prompt_builder.get_db_connection") as mock_conn:
        bundle = build_job_tailoring_prompt(
            "wa-l3", slot_frameworks=slot_frameworks, job_override=override
        )
    mock_conn.assert_not_called()
    return bundle


def test_builder_includes_company_context():
    """WhatsApp path (no DB): the three Lesson 3 keys ride along."""
    bundle = _whatsapp_bundle(
        {
            "job1": "optum",
            "job2": "blue_yonder",
            "job3": "accenture",
            "job4": "thomson_reuters",
        }
    )
    guidelines = bundle["tailoring_guidelines"]
    assert "error" not in bundle
    ctx = guidelines["company_context"]
    assert ctx["job1"] is None  # optum has no company entry
    assert ctx["job2"]["company_name"] == "Blue Yonder"
    assert "verified" in guidelines["company_context_policy"].lower()
    assert "Model Context Protocol" in guidelines["acronym_rule"]
    # Lesson 2 keys still present alongside (empty on the WhatsApp path).
    assert guidelines["retrieved_evidence"] == []


def test_builder_company_context_failure_is_fail_open(monkeypatch):
    """A broken company file must never break tailoring; the rule holds."""

    def _boom():
        raise RuntimeError("company file unreadable")

    monkeypatch.setattr("src.synthesis.prompt_builder.load_company_frameworks", _boom)
    bundle = _whatsapp_bundle(SLOTS)
    guidelines = bundle["tailoring_guidelines"]
    assert "error" not in bundle
    assert guidelines["company_context"] == {}
    assert "Model Context Protocol" in guidelines["acronym_rule"]
