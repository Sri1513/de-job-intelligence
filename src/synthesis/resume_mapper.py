# src/synthesis/resume_mapper.py
from typing import Dict, Any, List

# Synchronized deterministic profile defaults
STATIC_PROFILE = {
    "CANDIDATE_NAME": "Sri Omkar Dumpa",
    "EMAIL": "sri.omkar.d@gmail.com",
    "PHONE": "(951) 545-2146",
    "LINKEDIN": "linkedin.com/in/sri-omkar-58r4r4r8",
    "LOCATION": "United States",
    "SUMMARY_TITLE": "Professional Summary",
    "EDUCATION_MS": "Master of Science in Information Technology | 2024\nCalifornia Baptist University, Riverside, CA",
    "EDUCATION_BS": "Bachelor of Technology in Computer Science | 2016\nJNTUK, Andhra Pradesh, India"
}

BASE_BULLETS = {
    "herc": [
        "Architected and deployed Python-based ingestion workflows, routing high-volume operational and fleet data across AS400, Oracle OLTP, and Teradata into an AWS S3 lakehouse with 0% data loss across multi-terabyte datasets.",
        "Engineered near-real-time streaming telemetry pipelines using AWS Kinesis and Databricks Streaming, processing semi-structured JSON and XML sensor payloads for fleet activity reporting.",
        "Reduced daily batch processing runtimes by 25–30% by tuning PySpark partition strategies, optimizing Spark SQL joins, and transitioning full-table refreshes to incremental CDC patterns.",
        "Decreased pipeline rerun effort by 30–40% by modularizing monolithic Databricks transformation workflows into checkpointed, restartable stages with automated error handling.",
        "Modeled dimensional star schemas, fact tables, and materialized views in Teradata and SQL, accelerating reporting layer query performance for downstream analytics.",
        "Designed and delivered executive-grade interactive dashboards in Qlik Sense and Power BI, enabling operational decision-makers to track asset utilization and billing analytics.",
        "Implemented automated data validation protocols, including source-to-target reconciliation and null-rate detection routines, ensuring complete data consistency across operational data lakes.",
        "Orchestrated end-to-end data pipelines using Apache Airflow and SQL Server Agent, configuring dynamic DAG retries, SLA monitoring, and alerting thresholds."
    ],
    "blue_yonder": [
        "Built programmatic data ingestion microservices utilizing Python, FastAPI, and SQLAlchemy, securing and standardizing ERP JSON and CSV feeds into Azure Data Lake Storage Gen2 (ADLS Gen2).",
        "Reduced batch transformation runtimes by 25–30% across distributed Azure Databricks pipelines by consolidating iterative PySpark DataFrame transformations and column enrichment into unified single-pass operations.",
        "Automated cloud infrastructure provisioning and identity governance across Azure environments utilizing Terraform, Docker, and Azure Key Vault for centralized secrets management.",
        "Developed distributed data cleansing, deduplication, and aggregation pipelines in PySpark to process high-throughput supply chain and demand forecasting datasets.",
        "Optimized analytical queries and reduced compute spend across Snowflake and Exasol data warehouses by implementing partition pruning, clustering keys, and query profiling.",
        "Configured and managed multi-stage Apache Airflow DAGs to coordinate complex pipeline dependencies, automated failure retries, and operational SLA tracking.",
        "Designed enterprise analytics views and self-service reporting layers connecting Snowflake to Tableau and Power BI, facilitating stakeholder data adoption across distributed business teams.",
        "Led collaborative solution design sessions and technical workshops, producing comprehensive architecture documentation and mentoring junior engineers on cloud data best practices."
    ],
    "accenture": [
        "Accelerated pipeline execution times by 35–40% on multi-billion-row transactional datasets by diagnosing Spark UI bottlenecks, applying data salting, broadcast hash joins, and Adaptive Query Execution (AQE).",
        "Modernized legacy Ab Initio ETL graphs and mainframe workflows by designing modular Python ingestion microservices targeting an AWS S3 data lake architecture.",
        "Modeled dimensional schemas and loaded audit-ready financial and regulatory compliance datasets into Amazon Redshift, structuring optimized tables for analytical reporting.",
        "Replaced full-table merge patterns with partition-aware anti-joins and incremental append routines, drastically reducing compute resource consumption.",
        "Engineered automated source-to-target reconciliation algorithms and data quality validation scripts in SQL and Python, achieving 100% data audit compliance for regulatory inspections.",
        "Established multi-tiered data lake layers (Bronze, Silver, Gold) on AWS S3, cataloging schemas and technical metadata within AWS Glue Data Catalog for enterprise discoverability.",
        "Automated batch data pipeline workflows using Apache Airflow, integrating with Git and Jenkins CI/CD pipelines to streamline deployment of data artifacts.",
        "Implemented column-level data masking and role-based access control (RBAC) policies across Redshift tables, safeguarding sensitive financial records.",
        "Partnered with cross-functional consulting teams to deliver enterprise data solutions, guiding client workstreams through requirement gathering, user acceptance testing, and delivery oversight."
    ],
    "thomson_reuters": [
        "Extracted historical relational records and financial transactions from legacy Microsoft SQL Server databases into AWS S3 using Apache Sqoop on AWS EMR clusters.",
        "Authored scalable HiveQL and PySpark transformation scripts within the Hadoop and AWS ecosystems to cleanse and normalize structured and semi-structured datasets.",
        "Reduced batch write latency by 20–25% by tuning Parquet block and page sizes, applying Snappy compression, and eliminating S3 rename overhead through optimized partitioning.",
        "Developed AWS Glue and PySpark ETL jobs to aggregate sales, customer usage, and revenue metrics before loading curated analytical datasets into Amazon Redshift and Athena.",
        "Improved database reporting execution times by 40% through index optimization, table partitioning, and advanced SQL query tuning on Microsoft SQL Server and Redshift.",
        "Built and deployed executive BI dashboards and operational reports across Tableau and SSRS, guiding business stakeholders in tracking revenue performance.",
        "Implemented daily source-to-target row count verifications, duplicate checks, and missing record detection scripts to guarantee data integrity across extraction layers.",
        "Coordinated with business analysts and technology teams to translate complex financial business logic into robust, maintainable data engineering pipelines."
    ]
}

def clean_bullet(text: str) -> str:
    """Strips Markdown bolding and leading bullet characters."""
    cleaned = text.replace("**", "").replace("*", "").strip()
    if cleaned.startswith(("- ", "• ")):
        cleaned = cleaned[2:].strip()
    return cleaned

def map_resume_placeholders(llm_payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Transforms tailored LLM output into key-value replacement tokens
    for the Google Docs template, falling back defensively to verified base values.
    """
    replacements: Dict[str, str] = {f"{{{{{k}}}}}": v for k, v in STATIC_PROFILE.items()}

    # Professional Summary
    summary = llm_payload.get("professional_summary") or (
        "Analytics and Data Engineer with 8+ years of cumulative professional experience "
        "leading the design, delivery, and enterprise modernizations of scalable data architectures, "
        "cloud data platforms, and distributed processing solutions."
    )
    replacements["{{PROFESSIONAL_SUMMARY}}"] = clean_bullet(summary)

    # Technical Skills Categories
    skills = llm_payload.get("technical_skills", {})
    replacements["{{SKILLS_CLOUD}}"] = skills.get(
        "cloud",
        "AWS (S3, EMR, Athena, Kinesis, Glue, IAM, CloudWatch), Microsoft Azure (ADLS Gen2, Databricks, Key Vault, Monitor, RBAC)"
    )
    replacements["{{SKILLS_BIGDATA}}"] = skills.get(
        "bigdata",
        "Apache Spark, PySpark, Spark SQL, Databricks, Databricks Streaming, Delta Lake, HiveQL, Sqoop"
    )
    replacements["{{SKILLS_LANGUAGES}}"] = skills.get(
        "languages",
        "Python (FastAPI, SQLAlchemy, Pandas, NumPy), SQL, HiveQL"
    )
    replacements["{{SKILLS_DATABASES}}"] = skills.get(
        "databases",
        "Amazon Redshift, Snowflake, Teradata, Oracle OLTP, Microsoft SQL Server, Exasol, AS400"
    )
    replacements["{{SKILLS_DEVOPS}}"] = skills.get(
        "devops",
        "Apache Airflow, Docker, Terraform, Git, Jenkins, CI/CD, SQL Server Agent"
    )
    replacements["{{SKILLS_MODELING}}"] = skills.get(
        "modeling_tuning",
        "Star Schema, Snowflake Schema, Fact and Dimension Tables, SCD Type 2, CDC, Source-to-Target Reconciliation, Catalyst Tuning, AQE"
    )

    # Job Bullets Mapping (with fallback protection)
    experience = llm_payload.get("experience", {})

    company_mapping = [
        ("herc", "HERC", 8),
        ("blue_yonder", "BLUE_YONDER", 8),
        ("accenture", "ACCENTURE", 9),
        ("thomson_reuters", "THOMSON_REUTERS", 8)
    ]

    for key, prefix, required_count in company_mapping:
        provided_bullets: List[str] = experience.get(key, [])
        fallback_bullets = BASE_BULLETS[key]

        for i in range(1, required_count + 1):
            placeholder = f"{{{{{prefix}_BULLET_{i}}}}}"
            if i <= len(provided_bullets) and provided_bullets[i - 1].strip():
                replacements[placeholder] = clean_bullet(provided_bullets[i - 1])
            else:
                # Defensive fallback to static base bullet
                replacements[placeholder] = clean_bullet(fallback_bullets[i - 1])

    return replacements