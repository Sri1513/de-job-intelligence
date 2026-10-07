"""Phase 2: deterministic JD skill extraction."""
from src.engine.jd_skill_extractor import extract_jd_skills, skill_ids

SAMPLE_JD = """
Data Engineer - Healthcare

Requirements:
- 5+ years with PySpark and Apache Spark on AWS
- Strong SQL and T-SQL for SQL Server
- Experience with PHI masking and HIPAA compliance
- Airflow DAGs for orchestration

Preferred:
- dbt and Snowflake experience
- Kubernetes (k8s) exposure

Responsibilities:
- Build ETL pipelines migrating SSIS packages to the cloud
"""


def test_extracts_canonical_ids():
    ids = skill_ids(SAMPLE_JD)
    for expected in ["pyspark", "apache_spark", "aws", "sql", "tsql",
                     "sqlserver", "phi_masking", "hipaa", "airflow",
                     "ssis", "etl", "kubernetes", "snowflake", "dbt"]:
        assert expected in ids, f"missing {expected}"


def test_no_double_count_pyspark_as_spark():
    ids = skill_ids("We need PySpark developers.")
    assert "pyspark" in ids
    assert "apache_spark" not in ids


def test_alias_forms():
    ids = skill_ids("k8s and TF and Postgres required.")
    assert "kubernetes" in ids
    assert "terraform" in ids
    assert "postgres" in ids


def test_section_weighting():
    req = extract_jd_skills("Requirements:\n- PySpark\n\nPreferred:\n- dbt")
    weights = {r["canonical_id"]: r["weight"] for r in req}
    assert weights["pyspark"] > weights["dbt"]


def test_empty_jd():
    assert extract_jd_skills("") == []
    assert extract_jd_skills("   ") == []
