# tests/test_synthesis.py
from src.synthesis.resume_mapper import map_resume_placeholders, STATIC_PROFILE

def test_resume_mapper_fallback_resilience():
    # Simulate an incomplete LLM response missing several bullets and fields
    sparse_llm_payload = {
        "professional_summary": "Seasoned Data Engineer with **8+ years** scaling lakehouses.",
        "technical_skills": {
            "cloud": "AWS, Azure",
            "bigdata": "Apache Spark, Kafka"
        },
        "experience": {
            "herc": [
                "Built **high-throughput** telemetry pipelines using Databricks Streaming."
            ]
            # blue_yonder, accenture, thomson_reuters completely omitted
        }
    }

    mapped = map_resume_placeholders(sparse_llm_payload)

    # 1. Verify candidate profile truth preserved
    assert mapped["{{CANDIDATE_NAME}}"] == STATIC_PROFILE["CANDIDATE_NAME"]
    assert mapped["{{EMAIL}}"] == STATIC_PROFILE["EMAIL"]

    # 2. Verify markdown bolding stripped
    assert "**8+ years**" not in mapped["{{PROFESSIONAL_SUMMARY}}"]
    assert "8+ years" in mapped["{{PROFESSIONAL_SUMMARY}}"]

    # 3. Verify custom provided bullet applied cleanly
    assert mapped["{{HERC_BULLET_1}}"] == "Built high-throughput telemetry pipelines using Databricks Streaming."

    # 4. Verify defensive fallback for omitted bullets
    assert "{{HERC_BULLET_2}}" in mapped
    assert len(mapped["{{HERC_BULLET_2}}"]) > 30

    # 5. Verify omitted companies fell back to verified base bullets
    assert "{{BLUE_YONDER_BULLET_1}}" in mapped
    assert "{{ACCENTURE_BULLET_1}}" in mapped
    assert "{{THOMSON_REUTERS_BULLET_1}}" in mapped