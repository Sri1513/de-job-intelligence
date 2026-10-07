"""Phase 5: credibility audit + verify->revise loop."""
from src.engine.credibility_audit import audit_credibility, verify_and_revise
from src.engine.jd_skill_extractor import extract_jd_skills

SLOT_DATA = {
    "job1": {"bullet_bank": [
        {"id": "o1", "text": "Migrated 30+ DataStage jobs to AWS using PySpark.",
         "skills": ["IBM DataStage", "PySpark", "AWS"]},
        {"id": "o2", "text": "Cut load time from 7:30 AM to 3:00 AM.",
         "skills": ["Performance Tuning"]},
    ]},
    "job2": {"bullet_bank": []},
}

JD_SKILLS = extract_jd_skills("Requirements: PySpark, AWS.")


def test_clean_payload_passes():
    payload = {"summary": "Data Engineer with 7+ years.",
               "experience_bullets": {"job1": ["Migrated 30+ DataStage jobs to AWS using PySpark."]}}
    report = audit_credibility(payload, SLOT_DATA, ["pyspark", "aws"])
    assert report["clean"], report["violations"]


def test_invented_metric_flagged():
    payload = {"experience_bullets": {"job1": ["Migrated 30+ jobs, cutting cost by 99%."]}}
    report = audit_credibility(payload, SLOT_DATA, [])
    assert not report["clean"]
    assert any(v["type"] == "number" and v["value"] == "99%" for v in report["violations"])


def test_invented_tool_flagged():
    payload = {"experience_bullets": {"job1": ["Built pipelines with Apache Flink."]}}
    report = audit_credibility(payload, SLOT_DATA, [])
    assert not report["clean"]
    assert any(v["type"] == "tool" for v in report["violations"])


def test_jd_tool_allowed():
    payload = {"experience_bullets": {"job1": ["Migrated 30+ DataStage jobs with dbt."]}}
    report = audit_credibility(payload, SLOT_DATA, ["dbt"])
    assert report["clean"], report["violations"]


def test_verify_revise_loop_passes_eventually():
    calls = {"n": 0}

    def revise_fn(payload, audit, score):
        calls["n"] += 1
        # simulate the LLM fixing the invented metric
        return {"experience_bullets": {"job1": ["Migrated 30+ DataStage jobs to AWS using PySpark."]}}

    bad = {"summary": "Data Engineer with 7+ years on AWS.",
           "experience_bullets": {"job1": ["Migrated 30+ jobs, cutting cost by 99% with PySpark on AWS."]}}
    out = verify_and_revise(bad, SLOT_DATA, JD_SKILLS, revise_fn,
                            score_threshold=0, max_iters=3)
    assert out["passed"]
    assert out["iterations"] == 1


def test_fail_closed_replaces_violating_bullet():
    def stubborn(payload, audit, score):
        return payload  # never fixes anything

    bad = {"experience_bullets": {"job1": ["Cut cost by 99% with Flink."]}}
    out = verify_and_revise(bad, SLOT_DATA, JD_SKILLS, stubborn,
                            score_threshold=0, max_iters=2)
    assert out["iterations"] == 2
    fixed_bullet = out["payload"]["experience_bullets"]["job1"][0]
    assert "99%" not in fixed_bullet and "Flink" not in fixed_bullet
