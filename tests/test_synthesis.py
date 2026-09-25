# tests/test_synthesis.py
from src.synthesis.resume_mapper import STATIC_PROFILE, build_replacement_payload


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

    mapped = build_replacement_payload(sparse_llm_payload)

    # 1. Verify candidate profile truth preserved
    assert mapped["{{NAME}}"] == STATIC_PROFILE["{{NAME}}"]

    # 2. Verify markdown bolding stripped from summary
    assert "**8+ years**" not in mapped["{{SUMMARY}}"]
    assert "8+ years" in mapped["{{SUMMARY}}"]

    # 3. Verify custom provided bullet applied cleanly
    assert mapped["{{JOB1_BULLET_1}}"] == "Built high-throughput telemetry pipelines using Databricks Streaming."

    # 4. Verify defensive fallback for omitted bullets in Job 1
    assert "{{JOB1_BULLET_2}}" in mapped
    assert len(mapped["{{JOB1_BULLET_2}}"]) > 30

    # 5. Verify omitted companies (Job 2, 3, 4) fell back to verified base bullets
    assert "{{JOB2_BULLET_1}}" in mapped
    assert "{{JOB3_BULLET_1}}" in mapped
    assert "{{JOB4_BULLET_1}}" in mapped