# tests/test_prompt_builder.py
from src.core.utils import get_cached_resume
from src.engine.matcher import calculate_match_score
from src.synthesis.prompt_builder import load_company_frameworks, load_role_config


def test_configurations_load():
    frameworks = load_company_frameworks()
    assert "companies" in frameworks
    assert "herc_rentals" in frameworks["companies"]

    role_config = load_role_config("data_engineering")
    assert "role_title" in role_config
    assert "skills_schema" in role_config


def test_resume_caching():
    resume_text = get_cached_resume("data_engineering")
    assert len(resume_text) > 500
    assert "Candidate Profile & Matching Criteria" in resume_text
    assert "Core Technical Skills" in resume_text
    assert "California Baptist University" in resume_text


def test_skill_matcher():
    sample_jd = "Looking for a Senior Data Engineer with strong Python, Apache Spark, and Snowflake experience."
    result = calculate_match_score(sample_jd, "data_engineering")
    assert result["score"] > 20
    assert "python" in result["matched_core"]
    assert "spark" in result["matched_core"]


def test_build_tailoring_prompt_job_override_skips_db():
    """WhatsApp path: pre-parsed job dict must build the bundle with no saved_jobs lookup."""
    from unittest.mock import patch

    from src.synthesis.prompt_builder import build_job_tailoring_prompt

    override = {
        "job_id": "wa-abc123",
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
    slot_frameworks = {
        "job1": "optum",
        "job2": "blue_yonder",
        "job3": "accenture",
        "job4": "thomson_reuters",
    }
    with patch("src.synthesis.prompt_builder.get_db_connection") as mock_conn:
        bundle = build_job_tailoring_prompt(
            "wa-abc123", slot_frameworks=slot_frameworks, job_override=override
        )
    mock_conn.assert_not_called()
    assert "error" not in bundle
    assert bundle["job_metadata"]["company"] == "Ask Consulting"
    assert bundle["job_metadata"]["title"] == "Data Engineer"
    assert bundle["slot_frameworks"]["job1"] == "optum"
    assert "Healthcare" in bundle["job_description"] or "healthcare" in bundle["job_description"].lower()


def test_skill_equivalences_load():
    from src.synthesis.prompt_builder import load_skill_equivalences

    eq = load_skill_equivalences()
    assert eq["version"] >= 1
    m = eq["equivalences"]
    assert "scala" in m
    assert "PySpark" in m["scala"]["equivalent"]
    assert "databricks" in m
    assert "matillion" in m
    # every equivalent must be a non-empty list of real skill names
    for tool, entry in m.items():
        assert entry["equivalent"], f"{tool} has empty equivalent list"
        assert entry.get("why"), f"{tool} missing rationale"
        assert entry.get("confidence") in ("green", "yellow"), f"{tool} missing confidence tag"


def test_translation_policy_in_prompt():
    from src.synthesis.prompt_builder import TOOL_TRANSLATION_POLICY, FIDELITY_RULES

    assert "skill_equivalences" in TOOL_TRANSLATION_POLICY
    assert "PySpark" in TOOL_TRANSLATION_POLICY
    # the old hard-omit rules are gone
    assert "OMIT it" not in FIDELITY_RULES
    assert "ONLY skills listed in BASE RESUME" not in FIDELITY_RULES
    assert "TRANSLATE" in FIDELITY_RULES
