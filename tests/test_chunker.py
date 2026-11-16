"""Tests for Sprint 3, Lesson 1: the chunker.

JD side:
- section headers split the JD; the chunk_kind follows the shared
  section patterns (required / preferred / responsibilities / other).
- the header guard: a content line ending in a period is NOT a header,
  so "dbt and Terraform experience is a plus." stays in the chunks
  instead of being swallowed the way the raw splitter swallows it.
- empty descriptions produce no chunks.

Resume side (inline samples, not the real files — the backfill, not
the unit test, reads the real resume):
- ### headers parse into client / role / period; bullets become
  experience_bullet chunks with "<slug>:bullet:<n>" source_refs.
- one framework chunk per company process phase.
"""

from src.engine.chunker import (
    chunk_frameworks,
    chunk_jd,
    chunk_resume_evidence,
    parse_resume_markdown,
    split_jd_sections,
)

SAMPLE_JD = """About the role
You will build data pipelines and own the warehouse.

Requirements
5+ years of Python and SQL. Experience with Snowflake and Airflow.
Strong PySpark skills required.

Preferred qualifications
dbt and Terraform experience is a plus.
"""

SAMPLE_MD = """## Professional Experience

### Optum, Florida — Data Engineer, Oct 2025 - Present

- Led the migration of 30+ ETL workflows to AWS Glue and PySpark.
- Cut the nightly batch from 7:30 AM to 3:00 AM with Redshift COPY.

### Experis IT (Thomson Reuters), India — ETL Developer, June 2017 - Sep 2019

- Built Informatica mappings for sales reporting.
"""

SAMPLE_FW = {
    "companies": {
        "herc_rentals": {
            "process_framework": {
                "phase_1_ingestion": {
                    "phase_name": "Data Ingestion & Event Streaming",
                    "daily_activity": "Extracting records with Python and AWS Kinesis.",
                },
                "phase_2_storage": {
                    "phase_name": "Cloud Storage & Lakehouse Landing",
                    "daily_activity": "Landing datasets into S3 and Delta Lake.",
                },
            }
        }
    }
}


def test_split_jd_sections_keeps_preferred_content():
    sections = split_jd_sections(SAMPLE_JD)
    text = "\n".join(s["text"] for s in sections)
    assert "dbt and Terraform experience is a plus." in text
    kinds = [s["name"] for s in sections]
    assert kinds[0] == "responsibilities"
    assert "required" in kinds


def test_chunk_jd_tags_skills():
    chunks = chunk_jd("demo-1", SAMPLE_JD)
    assert len(chunks) == 3
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["job_id"] == "demo-1"
    required = chunks[1]
    assert required["chunk_kind"] == "required"
    assert "python" in required["skill_tags"]
    assert "snowflake" in required["skill_tags"]
    preferred = chunks[2]
    assert "dbt" in preferred["skill_tags"]
    assert "terraform" in preferred["skill_tags"]


def test_chunk_jd_empty():
    assert chunk_jd("x", "") == []
    assert chunk_jd("x", "   ") == []


def test_parse_resume_markdown():
    chunks = parse_resume_markdown(SAMPLE_MD)
    assert len(chunks) == 3
    first = chunks[0]
    assert first["source_ref"] == "optum:bullet:0"
    assert first["source_type"] == "experience_bullet"
    assert first["client"] == "Optum"
    assert first["role"] == "Data Engineer"
    assert first["period"] == "Oct 2025 - Present"
    assert "pyspark" in first["skill_tags"]
    # "AWS Glue" matches as one surface form -> canonical "glue";
    # the matcher consumes the span, so bare "aws" does not also match.
    assert "glue" in first["skill_tags"]
    assert chunks[1]["source_ref"] == "optum:bullet:1"
    third = chunks[2]
    assert third["source_ref"] == "thomson-reuters:bullet:0"
    assert third["client"] == "Thomson Reuters"
    assert third["role"] == "ETL Developer"


def test_chunk_frameworks():
    chunks = chunk_frameworks(SAMPLE_FW)
    assert len(chunks) == 2
    assert chunks[0]["source_ref"] == "framework:herc_rentals:phase_1_ingestion"
    assert chunks[0]["source_type"] == "framework"
    assert chunks[0]["framework_name"] == "Data Ingestion & Event Streaming"
    assert chunks[0]["client"] is None
    assert "python" in chunks[0]["skill_tags"]
    assert "kinesis" in chunks[0]["skill_tags"] or "aws" in chunks[0]["skill_tags"]


def test_chunk_resume_evidence_combines_both():
    chunks = chunk_resume_evidence(SAMPLE_MD, SAMPLE_FW)
    assert len(chunks) == 5
    assert chunks[0]["source_type"] == "experience_bullet"
    assert chunks[-1]["source_type"] == "framework"
