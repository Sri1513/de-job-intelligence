"""Phase 4: deterministic post-generation scorer."""
from src.engine.jd_skill_extractor import extract_jd_skills
from src.engine.resume_scorer import score_resume

JD = "Requirements: PySpark, AWS, SQL. Healthcare role needing HIPAA."

GOOD_RESUME = {
    "summary": "Data Engineer with 7+ years building PySpark pipelines on AWS.",
    "technical_skills": {"cloud": "AWS, S3", "bigdata": "PySpark, Spark SQL"},
    "experience_bullets": {
        "job1": ["Migrated data with PySpark on AWS, handling PHI under HIPAA."],
        "job2": ["Wrote SQL queries for reporting."],
    },
}

BAD_RESUME = {
    "summary": "Data Engineer with 7+ years.",
    "technical_skills": {"cloud": "GCP"},
    "experience_bullets": {"job1": ["Did some data work."]},
}


def test_good_resume_outscores_bad():
    jd_skills = extract_jd_skills(JD)
    good = score_resume(GOOD_RESUME, jd_skills)
    bad = score_resume(BAD_RESUME, jd_skills)
    assert good["score"] > bad["score"]
    assert good["score"] > 50
    assert any(m["skill"] == "pyspark" for m in good["matched"])
    assert any(m["skill"] == "hipaa" for m in good["matched"])


def test_missing_skills_reported():
    jd_skills = extract_jd_skills(JD)
    result = score_resume(BAD_RESUME, jd_skills)
    missing_ids = {m["skill"] for m in result["missing"]}
    assert "pyspark" in missing_ids
    assert result["coverage"] < 0.5


def test_stuffing_penalty():
    jd_skills = extract_jd_skills("Requirements: PySpark.")
    stuffed = {
        "summary": "PySpark PySpark PySpark PySpark PySpark expert.",
        "technical_skills": {"bigdata": "PySpark"},
        "experience_bullets": {"job1": ["PySpark work."]},
    }
    result = score_resume(stuffed, jd_skills)
    assert result["stuffing"], "expected a stuffing flag"
    assert result["stuffing_penalty"] > 0
    assert result["score"] < result["raw_score"]


def test_empty_jd_scores_zero():
    assert score_resume(GOOD_RESUME, [])["score"] == 0.0
