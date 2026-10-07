"""Phase 3: hybrid scoring, RRF fusion, coverage + MMR selection."""
from src.engine.hybrid_scorer import (
    bm25_scores,
    greedy_coverage,
    mmr_select,
    rrf_fuse,
    score_and_select,
)
from src.engine.jd_skill_extractor import extract_jd_skills

BANK = [
    {"id": "b1", "text": "Migrated SSIS packages to AWS S3.", "skills": ["Microsoft SSIS", "AWS"]},
    {"id": "b2", "text": "Built PySpark streaming pipelines on AWS.", "skills": ["PySpark", "Streaming", "AWS"]},
    {"id": "b3", "text": "Wrote SQL queries for reports.", "skills": ["SQL"]},
    {"id": "b4", "text": "More PySpark ETL work with Spark.", "skills": ["PySpark", "ETL"]},
]

JD = "Requirements: PySpark, AWS, SQL. Healthcare role."


def test_bm25_prefers_lexical_match():
    docs = [b["text"] for b in BANK] + [
        "Document about cooking recipes and baking cakes.",
        "Document about gardening tools and plant care.",
        "Document about car maintenance and oil changes.",
        "Document about travel itineraries and hotel bookings.",
    ]
    scores = bm25_scores(docs, ["pyspark"])
    # b2 (index 1) mentions PySpark; filler docs do not
    assert scores[1] > scores[4]
    assert scores[1] > scores[2]  # b2 mentions PySpark, b3 does not


def test_rrf_fusion_combines_rankings():
    fused = rrf_fuse([[3.0, 1.0, 2.0], [1.0, 3.0, 2.0]])
    # item 0 wins ranking A, item 1 wins ranking B -> both top, item 2 last
    assert fused[2] < fused[0] and fused[2] < fused[1]


def test_greedy_coverage_maximizes_skill_spread():
    jd_skills = extract_jd_skills(JD)
    weights = {s["canonical_id"]: s["weight"] for s in jd_skills}
    bullets = [{**b, "_skill_ids": ["pyspark"] if b["id"] in ("b2", "b4") else ["aws"],
                "_fused_score": 0.5} for b in BANK]
    picked = greedy_coverage(bullets, weights, 2)
    ids = {b["id"] for b in picked}
    # b2 covers pyspark+aws, b4 only pyspark -> b2 preferred for coverage
    assert "b2" in ids


def test_mmr_diversity_avoids_redundancy():
    bullets = [
        {"id": "a", "text": "Built PySpark ETL pipelines daily", "_fused_score": 1.0},
        {"id": "b", "text": "Built PySpark ETL pipelines nightly", "_fused_score": 0.80},
        {"id": "c", "text": "Migrated Oracle to Snowflake", "_fused_score": 0.75},
    ]
    picked = mmr_select(bullets, 2, lambda_mult=0.7)
    ids = [b["id"] for b in picked]
    assert ids[0] == "a"
    assert "c" in ids  # diverse pick beats near-duplicate b


def test_score_and_select_end_to_end():
    jd_skills = extract_jd_skills(JD)
    picked = score_and_select(BANK, JD, jd_skills, k=2)
    assert len(picked) == 2
    assert all("_fused_score" in b for b in picked)
