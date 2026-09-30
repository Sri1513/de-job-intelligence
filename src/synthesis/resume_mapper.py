# src/synthesis/resume_mapper.py
"""
src/synthesis/resume_mapper.py
Parses granular schemas, company-keyed dictionaries, and monolithic text blocks.
Guarantees all 66 Google Doc template placeholders are resolved without leaving unrendered tokens.
"""

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger("de-job-intelligence.synthesis")

STATIC_PROFILE = {
    "{{NAME}}": "Sri Omkar Dumpa",
    "{{CONTACT_BAR}}": "Email: sri.omkar.d@gmail.com | Phone: (951) 545-2146 | sriomkar.com | linkedin.com/in/sri-omkar-58r4r4r8",
    # Job 1 - Herc Rentals
    "{{JOB1_COMPANY}}": "Herc Rentals Inc.",
    "{{JOB1_LOCATION}}": "Bonita Springs, FL",
    "{{JOB1_TITLE}}": "Senior Data Engineer",
    "{{JOB1_DATES}}": "Nov 2024 – Present",
    # Job 2 - Blue Yonder
    "{{JOB2_COMPANY}}": "Blue Yonder",
    "{{JOB2_LOCATION}}": "Hyderabad, India",
    "{{JOB2_TITLE}}": "Data Engineer",
    "{{JOB2_DATES}}": "June 2022 – July 2023",
    # Job 3 - Accenture
    "{{JOB3_COMPANY}}": "Accenture",
    "{{JOB3_LOCATION}}": "Hyderabad, India",
    "{{JOB3_TITLE}}": "Data Engineer",
    "{{JOB3_DATES}}": "Jan 2020 – June 2022",
    # Job 4 - Thomson Reuters
    "{{JOB4_COMPANY}}": "Thomson Reuters",
    "{{JOB4_LOCATION}}": "Hyderabad, India",
    "{{JOB4_TITLE}}": "ETL Developer",
    "{{JOB4_DATES}}": "June 2016 – Sep 2019",
    # Education
    "{{EDU1_DEGREE}}": "Master of Science in Information Technology",
    "{{EDU1_YEAR}}": "2024",
    "{{EDU1_INSTITUTION}}": "California Baptist University",
    "{{EDU1_LOCATION}}": "Riverside, CA",
    "{{EDU2_DEGREE}}": "Bachelor of Technology in Computer Science",
    "{{EDU2_YEAR}}": "2016",
    "{{EDU2_INSTITUTION}}": "JNTUK",
    "{{EDU2_LOCATION}}": "Andhra Pradesh, India",
}

BASE_SKILLS = {
    "cloud": "AWS (S3, EMR, Athena, Kinesis, Glue, Redshift), Azure (ADLS Gen2, Databricks, Key Vault)",
    "bigdata": "Apache Spark, PySpark, Spark SQL, Databricks Streaming, Hadoop, HiveQL",
    "languages": "Python (FastAPI, SQLAlchemy, Pandas, NumPy), SQL, Bash",
    "databases": "Amazon Redshift, Snowflake, Teradata, PostgreSQL, MS SQL Server",
    "devops": "Apache Airflow, Docker, Terraform, Git, Jenkins, CI/CD, dbt",
    "modeling_tuning": "Dimensional Modeling (Star/Snowflake), SCD Type 2, CDC, AQE, Broadcast Joins",
}

BASE_BULLETS = {
    "JOB1": [
        "Accelerated high-volume data ingestion throughput by 35% across legacy systems (RentalMan, AS400, Oracle OLTP, and Teradata) into an AWS S3 data lake by architecting modular Python-based extraction services.",
        "Decreased batch pipeline runtimes by 25–30% across core fleet datasets by refactoring PySpark partition strategies, tuning Spark SQL join hints, and deploying incremental CDC patterns.",
        "Enhanced operational visibility for 50,000+ fleet assets by developing near-real-time streaming pipelines using AWS Kinesis and Databricks Streaming to ingest semi-structured JSON/XML telemetry data.",
        "Reduced pipeline recovery and rerun time by 35% during system interruptions by decoupling monolithic Databricks transformation chains into checkpointed, modular stages.",
        "Improved executive dashboard refresh speeds by 40% on Qlik Sense by designing dimensional data models, star schemas, and materialized reporting views on Teradata and S3.",
        "Enforced 99.8% data consistency across multi-source ingestion feeds by integrating automated data profiling, null validation checks, and schema enforcement scripts in Python.",
        "Enhanced enterprise security compliance by establishing AWS IAM least-privilege role policies and S3 bucket encryption protocols for sensitive fleet telemetry and operational data.",
        "Boosted team release velocity by 20% in an Agile sprint cadence by configuring automated Git version control workflows and reusable CI/CD pipeline templates.",
    ],
    "JOB2": [
        "Reduced batch transformation latency by 25–30% on multi-terabyte supply chain datasets by consolidating iterative DataFrame transformations into vectorized PySpark operations on Azure Databricks.",
        "Improved data ingestion reliability by 40% by building programmatic REST ingestion microservices using Python, FastAPI, and SQLAlchemy to stream client ERP datasets into ADLS Gen2.",
        "Decreased data processing defects by 30% by developing distributed data-cleaning and deduplication routines in Azure Databricks to handle complex, nested schemas.",
        "Mitigated pipeline downtime and eliminated manual interventions by orchestrating 25+ Apache Airflow DAGs with customized retry policies, SLA monitoring, and automated alerting.",
        "Strengthened cloud security posture by provisioning infrastructure via Terraform and centralizing credential and secret management in Azure Key Vault.",
        "Reduced source-to-target data discrepancies by 99% by engineering automated reconciliation audits comparing ADLS Gen2 raw layers against curated data warehouse tables.",
        "Lowered Azure Databricks cluster compute expenses by 15% by implementing auto-scaling cluster configurations and optimizing node size allocations based on workload demands.",
        "Accelerated cross-functional feature deployment cycles by 25% by collaborating with product managers, data scientists, and analysts within two-week Agile sprints.",
    ],
    "JOB3": [
        "Reduced financial pipeline execution runtime by 35–40% across terabyte-scale transaction logs by diagnosing data skew via the Spark UI and implementing data salting, broadcast joins, and Adaptive Query Execution (AQE).",
        "Accelerated daily incremental data processing by 30% by replacing legacy full-table merges with partition-aware anti-join and append patterns in PySpark.",
        "Facilitated enterprise legacy modernization by migrating mission-critical data processing workflows from on-premises Ab Initio systems to automated Python and Spark pipelines on AWS S3.",
        "Improved reporting query latency by 45% for Financial Crime analytics teams by designing dimensional star schemas and loading audit-ready tables into Amazon Redshift.",
        "Decreased compliance audit preparation time by 50% by establishing comprehensive source-to-target mapping documentation, data dictionaries, and automated reconciliation checks.",
        "Achieved 99.9% pipeline uptime across critical banking workflows by scheduling and monitoring production Apache Airflow DAGs with automated alerting.",
        "Minimized deployment defects by 30% by establishing standardized CI/CD deployment pipelines using Git, Jenkins, and automated testing suites.",
        "Safeguarded sensitive PII and financial records in compliance with regulatory standards by applying column-level encryption, data masking, and role-based access controls (RBAC).",
        "Resolved 20+ critical operational data pipeline bottlenecks by conducting deep-dive root cause analyses and performance profiling across distributed Spark clusters.",
    ],
    "JOB4": [
        "Modernized legacy data extraction workflows by designing and deploying Apache Sqoop extraction jobs on AWS EMR to ingest historical relational records from MS SQL Server into Amazon S3.",
        "Reduced batch query runtimes by 25% on large-scale relational and flat-file datasets by authoring optimized HiveQL scripts and partitioning tables within the Hadoop ecosystem.",
        "Cut batch write latency by 20–25% by tuning Parquet block and page sizes and optimizing S3 multi-part upload parameters for downstream Spark analytics workloads.",
        "Shortened executive reporting turnaround by 30% by building AWS Glue and PySpark jobs to aggregate complex sales and revenue metrics prior to loading into Amazon Redshift and Athena.",
        "Eliminated downstream data corruption incidents by implementing automated source-to-target row count verifications, checksums, and schema validation checkpoints.",
        "Improved query response times for financial analysts by 35% by implementing column-level compression, sorting keys, and distribution styles in Amazon Redshift.",
        "Enhanced pipeline maintainability and handover efficiency by authoring comprehensive data flow diagrams, operational runbooks, and source-to-target transformation specifications.",
        "Delivered timely data engineering deliverables across cross-functional engineering pods by actively participating in daily Agile stand-ups, backlog grooming, and sprint reviews.",
    ],
}


def clean_text(val: Any) -> str:
    """Removes markdown artifacts and leading bullet symbols."""
    text = str(val or "")
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"^[•\-\*\s]+", "", text).strip()
    return text


def parse_experience_text(raw_exp: Any) -> List[str]:
    """Extracts clean bullet strings from raw text or list payloads."""
    if not raw_exp:
        return []

    bullets = []
    if isinstance(raw_exp, list):
        for item in raw_exp:
            cleaned = clean_text(item)
            if cleaned:
                bullets.append(cleaned)
    elif isinstance(raw_exp, str):
        for line in raw_exp.split("\n"):
            cleaned = clean_text(line)
            if cleaned and not any(
                h in cleaned.lower()
                for h in [
                    "rentals",
                    "yonder",
                    "accenture",
                    "reuters",
                    "experience",
                    "professional experience",
                ]
            ):
                bullets.append(cleaned)
    return bullets


def parse_skills_text(skills_val: Any) -> Dict[str, str]:
    """Extracts categorized skill sets from either dict or raw string blocks."""
    parsed = dict(BASE_SKILLS)
    if not skills_val:
        return parsed

    if isinstance(skills_val, dict):
        for k in parsed:
            val = skills_val.get(k) or skills_val.get(k.replace("_", ""))
            if val:
                parsed[k] = clean_text(val)
        return parsed

    if isinstance(skills_val, str):
        for line in skills_val.split("\n"):
            line_clean = clean_text(line)
            lower = line_clean.lower()
            if "cloud" in lower and ":" in line_clean:
                parsed["cloud"] = line_clean.split(":", 1)[1].strip()
            elif "big data" in lower and ":" in line_clean:
                parsed["bigdata"] = line_clean.split(":", 1)[1].strip()
            elif "language" in lower and ":" in line_clean:
                parsed["languages"] = line_clean.split(":", 1)[1].strip()
            elif "database" in lower and ":" in line_clean:
                parsed["databases"] = line_clean.split(":", 1)[1].strip()
            elif ("devops" in lower or "orchestration" in lower) and ":" in line_clean:
                parsed["devops"] = line_clean.split(":", 1)[1].strip()
            elif ("modeling" in lower or "tuning" in lower) and ":" in line_clean:
                parsed["modeling_tuning"] = line_clean.split(":", 1)[1].strip()
    return parsed


def build_replacement_payload(dynamic_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Constructs the complete 66-token map for Google Docs API substitution.
    Resolves nested experience keys and falls back to verified profile defaults.
    """
    replacements = dict(STATIC_PROFILE)

    # 1. Professional Summary
    summary = dynamic_data.get("summary") or dynamic_data.get("professional_summary", "")
    if not summary:
        summary = (
            "Senior Data Engineer with 8+ years of experience designing and operating high-throughput "
            "distributed architectures, cloud data lakehouses, and real-time streaming pipelines across AWS and Azure."
        )
    replacements["{{SUMMARY}}"] = clean_text(summary)

    # 2. Technical Skills Matrix
    raw_skills = (
        dynamic_data.get("technical_skills")
        or dynamic_data.get("skills")
        or dynamic_data.get("skills_text")
    )
    skills_map = parse_skills_text(raw_skills)
    replacements["{{SKILLS_CLOUD}}"] = skills_map["cloud"]
    replacements["{{SKILLS_BIGDATA}}"] = skills_map["bigdata"]
    replacements["{{SKILLS_LANGUAGES}}"] = skills_map["languages"]
    replacements["{{SKILLS_DATABASES}}"] = skills_map["databases"]
    replacements["{{SKILLS_DEVOPS}}"] = skills_map["devops"]
    replacements["{{SKILLS_MODELING_TUNING}}"] = skills_map["modeling_tuning"]

    # 3. Resolve Experience Bullets across structured keys & fuzzy dictionary payloads
    j1, j2, j3, j4 = [], [], [], []

    if dynamic_data.get("job1_bullets"):
        j1 = dynamic_data.get("job1_bullets")
    if dynamic_data.get("job2_bullets"):
        j2 = dynamic_data.get("job2_bullets")
    if dynamic_data.get("job3_bullets"):
        j3 = dynamic_data.get("job3_bullets")
    if dynamic_data.get("job4_bullets"):
        j4 = dynamic_data.get("job4_bullets")

    exp_dict = (
        dynamic_data.get("experience_bullets")
        or dynamic_data.get("experience")
        or dynamic_data.get("roles")
        or {}
    )

    if isinstance(exp_dict, dict):
        for k, v in exp_dict.items():
            kl = k.lower()
            if not j1 and ("job1" in kl or "herc" in kl):
                j1 = v
            elif not j2 and ("job2" in kl or "blue" in kl or "yonder" in kl):
                j2 = v
            elif not j3 and ("job3" in kl or "accenture" in kl):
                j3 = v
            elif not j4 and ("job4" in kl or "thomson" in kl or "reuters" in kl):
                j4 = v

    if not any([j1, j2, j3, j4]):
        raw_list = (
            exp_dict if isinstance(exp_dict, list) else dynamic_data.get("experience_bullets")
        )
        all_parsed = parse_experience_text(raw_list)
        if len(all_parsed) >= 25:
            j1 = all_parsed[0:8]
            j2 = all_parsed[8:16]
            j3 = all_parsed[16:25]
            j4 = all_parsed[25:33]
        elif len(all_parsed) > 0:
            j1 = all_parsed[0 : min(len(all_parsed), 8)]

    job_configs = [
        ("JOB1", parse_experience_text(j1), 8),
        ("JOB2", parse_experience_text(j2), 8),
        ("JOB3", parse_experience_text(j3), 9),
        ("JOB4", parse_experience_text(j4), 8),
    ]

    for prefix, bullet_list, count in job_configs:
        base_list = BASE_BULLETS[prefix]
        for i in range(1, count + 1):
            # FIXED: Removed the underscore between BULLET and {i} to match template {{JOB1_BULLET1}}
            key = "{{" + f"{prefix}_BULLET{i}" + "}}"
            idx = i - 1
            if idx < len(bullet_list) and bullet_list[idx]:
                replacements[key] = bullet_list[idx]
            elif idx < len(base_list):
                replacements[key] = base_list[idx]
            else:
                replacements[key] = ""

    print(
        f"[Mapper] Injected tailored bullets: "
        f"JOB1={len(parse_experience_text(j1))}/8, "
        f"JOB2={len(parse_experience_text(j2))}/8, "
        f"JOB3={len(parse_experience_text(j3))}/9, "
        f"JOB4={len(parse_experience_text(j4))}/8"
    )

    return replacements


def map_resume_placeholders(dynamic_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Alias for build_replacement_payload to maintain compatibility with test suites.
    """
    return build_replacement_payload(dynamic_data)
