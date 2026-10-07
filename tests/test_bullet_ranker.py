"""Phase 2: deterministic bullet ranking by canonical skill overlap."""
from src.engine.bullet_ranker import rank_all_slots, rank_slot_bullets
from src.engine.jd_skill_extractor import extract_jd_skills

BANK = [
    {"id": "b1", "text": "Migrated SSIS packages to AWS.", "skills": ["Microsoft SSIS", "AWS"]},
    {"id": "b2", "text": "Built PySpark streaming.", "skills": ["PySpark", "Streaming"]},
    {"id": "b3", "text": "Wrote SQL queries.", "skills": ["SQL"]},
]

JD = "Requirements: PySpark, AWS, and SQL for a healthcare data role with PHI."


def test_ranking_prefers_jd_overlap():
    jd_skills = extract_jd_skills(JD)
    ranked = rank_slot_bullets(BANK, jd_skills)
    # b2 matches pyspark (1.5); b1 matches aws (1.5); b3 matches sql (1.5)
    # all score > 0; order among them follows weight then bank order
    assert all(r["_overlap_score"] > 0 for r in ranked)
    assert ranked[0]["_matched_skills"], "top bullet must show matched skills"


def test_no_overlap_scores_zero():
    jd_skills = extract_jd_skills("Requirements: COBOL and mainframe.")
    ranked = rank_slot_bullets(BANK, jd_skills)
    assert all(r["_overlap_score"] == 0 for r in ranked)


def test_rank_all_slots_shape():
    slot_data = {"job1": {"bullet_bank": BANK}, "job2": {"bullet_bank": []}}
    out = rank_all_slots(slot_data, JD)
    assert set(out.keys()) == {"job1", "job2"}
    assert len(out["job1"]) == 3
    assert out["job2"] == []


def test_prompt_builder_includes_deterministic_ranking():
    from unittest.mock import patch

    from src.synthesis.prompt_builder import build_job_tailoring_prompt

    override = {
        "job_id": "wa-x", "title": "Data Engineer", "company": "Ask Consulting",
        "location": "United States", "is_remote": True, "job_url": "",
        "description": JD, "job_category": "data_engineering",
        "metadata": {}, "notes": "",
    }
    slots = {"job1": "optum", "job2": "blue_yonder",
             "job3": "accenture", "job4": "thomson_reuters"}
    with patch("src.synthesis.prompt_builder.get_db_connection") as mock_conn:
        bundle = build_job_tailoring_prompt("wa-x", slot_frameworks=slots, job_override=override)
    mock_conn.assert_not_called()
    ranking = bundle["tailoring_guidelines"]["deterministic_bullet_ranking"]
    assert "job1" in ranking and len(ranking["job1"]) > 0
    top = ranking["job1"][0]
    assert {"id", "score", "matched"} <= set(top.keys())
