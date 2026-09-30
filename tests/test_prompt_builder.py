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
