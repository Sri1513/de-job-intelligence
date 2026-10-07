# tests/test_synthesis.py
from src.synthesis.resume_mapper import STATIC_PROFILE, build_replacement_payload


def test_resume_mapper_fallback_resilience():
    # Simulate an incomplete LLM response missing several bullets and fields
    sparse_llm_payload = {
        "professional_summary": "Seasoned Data Engineer with **8+ years** scaling lakehouses.",
        "technical_skills": {
            "cloud": "AWS, Azure",
            "bigdata": "Apache Spark, Kafka",
        },
        "experience": {
            "herc": ["Built **high-throughput** telemetry pipelines using Databricks Streaming."]
            # blue_yonder, accenture, thomson_reuters completely omitted
        },
    }

    mapped = build_replacement_payload(sparse_llm_payload)

    # 1. Verify candidate profile truth preserved
    assert mapped["{{NAME}}"] == STATIC_PROFILE["{{NAME}}"]

    # 2. Verify markdown bolding stripped from summary
    assert "**8+ years**" not in mapped["{{SUMMARY}}"]
    assert "8+ years" in mapped["{{SUMMARY}}"]

    # 3. Verify custom provided bullet applied cleanly
    assert (
        mapped["{{JOB1_BULLET1}}"]
        == "Built high-throughput telemetry pipelines using Databricks Streaming."
    )

    # 4. Verify defensive fallback for omitted bullets in Job 1
    assert "{{JOB1_BULLET2}}" in mapped
    assert len(mapped["{{JOB1_BULLET2}}"]) > 30

    # 5. Verify omitted companies (Job 2, 3, 4) fell back to verified base bullets
    assert "{{JOB2_BULLET1}}" in mapped
    assert "{{JOB3_BULLET1}}" in mapped
    assert "{{JOB4_BULLET1}}" in mapped


def test_slot_headers_override_static_fallback():
    # Healthcare JD -> slot 1 must stamp Akkodis (Optum), not the static fallback
    slot_headers = {
        "job1": {
            "header": "Akkodis (Optum)",
            "title": "Data Engineer",
            "location": "Florida",
            "dates": "Nov 2024 – Present",
        },
        "job2": {
            "header": "Quess (Blue Yonder)",
            "title": "Data Engineer",
            "location": "Hyderabad, India",
            "dates": "June 2022 – July 2023",
        },
    }
    mapped = build_replacement_payload({}, slot_headers=slot_headers)
    assert mapped["{{JOB1_COMPANY}}"] == "Akkodis (Optum)"
    assert mapped["{{JOB1_TITLE}}"] == "Data Engineer"
    assert mapped["{{JOB1_LOCATION}}"] == "Florida"
    assert mapped["{{JOB1_DATES}}"] == "Nov 2024 – Present"
    assert mapped["{{JOB2_COMPANY}}"] == "Quess (Blue Yonder)"
    # Slots without an override keep the static fallback
    assert mapped["{{JOB3_COMPANY}}"] == STATIC_PROFILE["{{JOB3_COMPANY}}"]


def test_mapper_static_fallback_is_truthful():
    mapped = build_replacement_payload({})
    # Name must be the real one, never the old full-name variant
    assert mapped["{{NAME}}"] == "Sri Omkar D"
    # Default slot-1 fallback follows the registry default (herc_rentals)
    assert mapped["{{JOB1_COMPANY}}"] == "Akkodis (Herc Rentals)"
    # Unsupported skills must not leak in via the default skills matrix
    assert "dbt" not in mapped["{{SKILLS_DEVOPS}}"].lower()


def test_map_resume_placeholders_passes_slot_headers():
    slot_headers = {"job1": {"header": "Akkodis (Optum)"}}
    from src.synthesis.resume_mapper import map_resume_placeholders

    mapped = map_resume_placeholders({}, slot_headers=slot_headers)
    assert mapped["{{JOB1_COMPANY}}"] == "Akkodis (Optum)"
