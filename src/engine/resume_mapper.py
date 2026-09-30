"""
src/resume_mapper.py
Parses both granular JSON schemas and monolithic text blocks (experience_bullets, skills_text),
guaranteeing that template placeholders are always populated.
"""

import re

STATIC_PROFILE = {
    "{{NAME}}": "Sri Omkar Dumpa",
    "{{CONTACT_BAR}}": "Email: sri.omkar.d@gmail.com | Phone: (951) 545-2146 | linkedin.com/in/sri-omkar-58r4r4r8",

    # Job 1
    "{{JOB1_COMPANY}}": "Herc Rentals Inc.",
    "{{JOB1_LOCATION}}": "Bonita Springs, FL",
    "{{JOB1_TITLE}}": "Senior Data Engineer",
    "{{JOB1_DATES}}": "Nov 2024 – Present",

    # Job 2
    "{{JOB2_COMPANY}}": "Blue Yonder",
    "{{JOB2_LOCATION}}": "Hyderabad, India",
    "{{JOB2_TITLE}}": "Data Engineer",
    "{{JOB2_DATES}}": "June 2022 – July 2023",

    # Job 3
    "{{JOB3_COMPANY}}": "Accenture",
    "{{JOB3_LOCATION}}": "Hyderabad, India",
    "{{JOB3_TITLE}}": "Data Engineer",
    "{{JOB3_DATES}}": "Jan 2020 – June 2022",

    # Job 4
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
    "{{EDU2_LOCATION}}": "Andhra Pradesh, India"
}

BASE_SKILLS = {
    "cloud": "AWS (S3, EMR, Athena, Kinesis, Glue, Redshift), Azure (ADLS Gen2, Databricks, Key Vault)",
    "bigdata": "Apache Spark, PySpark, Spark SQL, Databricks Streaming, Hadoop, HiveQL",
    "languages": "Python (FastAPI, SQLAlchemy, Pandas, NumPy), SQL, Bash",
    "databases": "Amazon Redshift, Snowflake, Teradata, PostgreSQL, MS SQL Server",
    "devops": "Apache Airflow, Docker, Terraform, Git, Jenkins, CI/CD, dbt",
    "modeling_tuning": "Dimensional Modeling (Star/Snowflake), SCD Type 2, CDC, AQE, Broadcast Joins"
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
        "Boosted team release velocity by 20% in an Agile sprint cadence by configuring automated Git version control workflows and reusable CI/CD pipeline templates."
    ],
    "JOB2": [
        "Reduced batch transformation latency by 25–30% on multi-terabyte supply chain datasets by consolidating iterative DataFrame transformations into vectorized PySpark operations on Azure Databricks.",
        "Improved data ingestion reliability by 40% by building programmatic REST ingestion microservices using Python, FastAPI, and SQLAlchemy to stream client ERP datasets into ADLS Gen2.",
        "Decreased data processing defects by 30% by developing distributed data-cleaning and deduplication routines in Azure Databricks to handle complex, nested schemas.",
        "Mitigated pipeline downtime and eliminated manual interventions by orchestrating 25+ Apache Airflow DAGs with customized retry policies, SLA monitoring, and automated alerting.",
        "Strengthened cloud security posture by provisioning infrastructure via Terraform and centralizing credential and secret management in Azure Key Vault.",
        "Reduced source-to-target data discrepancies by 99% by engineering automated reconciliation audits comparing ADLS Gen2 raw layers against curated data warehouse tables.",
        "Lowered Azure Databricks cluster compute expenses by 15% by implementing auto-scaling cluster configurations and optimizing node size allocations based on workload demands.",
        "Accelerated cross-functional feature deployment cycles by 25% by collaborating with product managers, data scientists, and analysts within two-week Agile sprints."
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
        "Resolved 20+ critical operational data pipeline bottlenecks by conducting deep-dive root cause analyses and performance profiling across distributed Spark clusters."
    ],
    "JOB4": [
        "Modernized legacy data extraction workflows by designing and deploying Apache Sqoop extraction jobs on AWS EMR to ingest historical relational records from MS SQL Server into Amazon S3.",
        "Reduced batch query runtimes by 25% on large-scale relational and flat-file datasets by authoring optimized HiveQL scripts and partitioning tables within the Hadoop ecosystem.",
        "Cut batch write latency by 20–25% by tuning Parquet block and page sizes and optimizing S3 multi-part upload parameters for downstream Spark analytics workloads.",
        "Shortened executive reporting turnaround by 30% by building AWS Glue and PySpark jobs to aggregate complex sales and revenue metrics prior to loading into Amazon Redshift and Athena.",
        "Eliminated downstream data corruption incidents by implementing automated source-to-target row count verifications, checksums, and schema validation checkpoints.",
        "Improved query response times for financial analysts by 35% by implementing column-level compression, sorting keys, and distribution styles in Amazon Redshift.",
        "Enhanced pipeline maintainability and handover efficiency by authoring comprehensive data flow diagrams, operational runbooks, and source-to-target transformation specifications.",
        "Delivered timely data engineering deliverables across cross-functional engineering pods by actively participating in daily Agile stand-ups, backlog grooming, and sprint reviews."
    ]
}

def parse_experience_text(raw_exp) -> list:
    """Extracts clean bullet strings from raw text or list payloads."""
    if not raw_exp:
        return []
    
    bullets = []
    if isinstance(raw_exp, list):
        for item in raw_exp:
            cleaned = re.sub(r'^[•\-\*\s]+', '', str(item)).strip()
            if cleaned:
                bullets.append(cleaned)
    elif isinstance(raw_exp, str):
        for line in raw_exp.split("\n"):
            cleaned = re.sub(r'^[•\-\*\s]+', '', line).strip()
            # Ignore headers (e.g., 'Herc Rentals Inc.', 'Professional Experience')
            if cleaned and not any(h in cleaned.lower() for h in ["rentals", "yonder", "accenture", "reuters", "experience"]):
                bullets.append(cleaned)
    return bullets

def parse_skills_text(skills_val) -> dict:
    """Extracts categorized skill sets from either dict or raw string blocks."""
    parsed = dict(BASE_SKILLS)
    if not skills_val:
        return parsed

    if isinstance(skills_val, dict):
        for k in parsed.keys():
            if skills_val.get(k):
                parsed[k] = skills_val[k]
        return parsed

    if isinstance(skills_val, str):
        lines = skills_val.split("\n")
        for line in lines:
            line_clean = re.sub(r'^[•\-\*\s]+', '', line).strip()
            lower = line_clean.lower()
            if "cloud" in lower and ":" in line_clean:
                parsed["cloud"] = line_clean.split(":", 1)[1].strip()
            elif "big data" in lower and ":" in line_clean:
                parsed["bigdata"] = line_clean.split(":", 1)[1].strip()
            elif "language" in lower and ":" in line_clean:
                parsed["languages"] = line_clean.split(":", 1)[1].strip()
            elif "database" in lower and ":" in line_clean:
                parsed["databases"] = line_clean.split(":", 1)[1].strip()
            elif "devops" in lower or "orchestration" in lower and ":" in line_clean:
                parsed["devops"] = line_clean.split(":", 1)[1].strip()
            elif "modeling" in lower or "tuning" in lower and ":" in line_clean:
                parsed["modeling_tuning"] = line_clean.split(":", 1)[1].strip()
    return parsed

def build_replacement_payload(dynamic_data: dict) -> dict:
    replacements = dict(STATIC_PROFILE)

    # 1. Summary
    summary = dynamic_data.get("summary") or dynamic_data.get("professional_summary", "")
    replacements["{{SUMMARY}}"] = str(summary).strip()

    # 2. Skills (Handles 'skills' dict or 'skills_text' string)
    raw_skills = dynamic_data.get("skills") or dynamic_data.get("skills_text")
    skills_map = parse_skills_text(raw_skills)
    replacements["{{SKILLS_CLOUD}}"] = skills_map["cloud"]
    replacements["{{SKILLS_BIGDATA}}"] = skills_map["bigdata"]
    replacements["{{SKILLS_LANGUAGES}}"] = skills_map["languages"]
    replacements["{{SKILLS_DATABASES}}"] = skills_map["databases"]
    replacements["{{SKILLS_DEVOPS}}"] = skills_map["devops"]
    replacements["{{SKILLS_MODELING_TUNING}}"] = skills_map["modeling_tuning"]

    # 3. Bullets (Check granular keys first, then fallback to monolithic parsing)
    j1 = dynamic_data.get("job1_bullets") or []
    j2 = dynamic_data.get("job2_bullets") or []
    j3 = dynamic_data.get("job3_bullets") or []
    j4 = dynamic_data.get("job4_bullets") or []

    # If granular lists were omitted, parse 'experience_bullets'
    if not any([j1, j2, j3, j4]):
        all_parsed = parse_experience_text(dynamic_data.get("experience_bullets"))
        print(f"🔍 [Mapper Fallback] Parsed {len(all_parsed)} lines from 'experience_bullets'")
        if len(all_parsed) >= 25:
            j1 = all_parsed[0:8]
            j2 = all_parsed[8:16]
            j3 = all_parsed[16:25]
            j4 = all_parsed[25:33]
        elif len(all_parsed) > 0:
            # Distribute whatever was sent
            j1 = all_parsed[0:min(len(all_parsed), 8)]

    job_configs = [
        ("JOB1", j1, 8),
        ("JOB2", j2, 8),
        ("JOB3", j3, 9),
        ("JOB4", j4, 8)
    ]

    for prefix, bullet_list, count in job_configs:
        base_list = BASE_BULLETS[prefix]
        for i in range(1, count + 1):
            key = f"{{{{{prefix}_BULLET{i}}}}}"
            idx = i - 1
            if idx < len(bullet_list) and str(bullet_list[idx]).strip():
                replacements[key] = str(bullet_list[idx]).strip()
            elif idx < len(base_list):
                # Fallback to proven base bullet
                replacements[key] = base_list[idx]
            else:
                replacements[key] = ""

    return replacements
