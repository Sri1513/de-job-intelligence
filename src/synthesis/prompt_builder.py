from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from src.core.config import settings
from src.core.database import get_db_connection

logger = logging.getLogger(__name__)

CONFIG_DIR = getattr(settings, "CONFIG_DIR", Path(__file__).resolve().parents[2] / "config")

_RESUME_CACHE: dict[str, str] = {}
_ROLE_CACHE: dict[str, dict] = {}
_FRAMEWORK_CACHE: dict[str, dict] = {}


def load_company_frameworks() -> dict:
    global _FRAMEWORK_CACHE
    if not _FRAMEWORK_CACHE:
        framework_file = CONFIG_DIR / "frameworks" / "company_frameworks.json"
        if framework_file.exists():
            try:
                with open(framework_file, "r", encoding="utf-8") as f:
                    _FRAMEWORK_CACHE = json.load(f)
            except Exception as e:
                logger.error(f"Error loading {framework_file}: {e}")
        else:
            logger.warning(f"Framework file not found at {framework_file}")
    return _FRAMEWORK_CACHE


def load_role_config(role_slug: str) -> dict:
    global _ROLE_CACHE
    if role_slug not in _ROLE_CACHE:
        slug_aliases = {
            "data_engineer": "data_engineering",
            "devops": "devops_engineering",
            "devops_engineer": "devops_engineering",
        }
        resolved_slug = slug_aliases.get(role_slug, role_slug)

        role_file = CONFIG_DIR / "roles" / f"{resolved_slug}.json"
        fallback_file = CONFIG_DIR / "roles" / "data_engineering.json"

        target_file = role_file if role_file.exists() else fallback_file

        if not target_file.exists():
            logger.warning(f"Role config missing at {target_file}. Using baseline boundaries.")
            return {
                "role_title": "Data Engineer",
                "boundaries": "Focus on distributed data engineering, streaming, and lakehouse platforms.",
                "bridging_rules": "Map target tools into their functional architectural phases.",
                "skills_schema": {},
            }

        try:
            with open(target_file, "r", encoding="utf-8") as f:
                _ROLE_CACHE[role_slug] = json.load(f)
        except Exception as e:
            logger.error(f"Error reading role config {target_file}: {e}")
            _ROLE_CACHE[role_slug] = {}

    return _ROLE_CACHE[role_slug]


def load_target_resume(role_slug: str) -> str:
    global _RESUME_CACHE
    resume_file_suffix = "devops" if "devops" in role_slug else "de"

    if resume_file_suffix not in _RESUME_CACHE:
        resume_path = CONFIG_DIR / "resumes" / f"resume_{resume_file_suffix}.md"
        if resume_path.exists():
            try:
                _RESUME_CACHE[resume_file_suffix] = resume_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.error(f"Error reading resume at {resume_path}: {e}")
                _RESUME_CACHE[resume_file_suffix] = ""
        else:
            logger.warning(f"Resume file not found at {resume_path}")
            _RESUME_CACHE[resume_file_suffix] = (
                "Senior Data Engineer with 8+ years of experience across Apache Spark, PySpark, "
                "Python, SQL, AWS, Azure, Snowflake, Databricks, and Lakehouse architectures."
            )

    return _RESUME_CACHE[resume_file_suffix]


def build_tailoring_prompt(job_id: str) -> Dict[str, Any]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, title, company, location, is_remote, job_url,
                       description, job_category, metadata, notes
                FROM saved_jobs
                WHERE job_id = %s;
                """,
                (job_id,),
            )
            job = cur.fetchone()

    if not job:
        return {"error": f"No job found in the database for job_id '{job_id}'."}

    category = (job.get("job_category") or "").lower()
    title_lower = (job.get("title") or "").lower()
    devops_keywords = [
        "devops", "cloud", "infrastructure", "platform",
        "sre", "reliability", "kubernetes", "terraform",
    ]

    if "devops" in category or any(kw in title_lower for kw in devops_keywords):
        role_slug = "devops"
    else:
        role_slug = "data_engineering"

    meta = job.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    target_tech_stack = meta.get("ai_extracted_skills") or []
    target_signals = meta.get("tailoring_signals") or []

    role_config = load_role_config(role_slug)
    company_frameworks = load_company_frameworks()
    base_resume = load_target_resume(role_slug)

    formatting_rules = (
        "PURPOSEFUL RECRUITER HIGHLIGHTING RULES (MANDATORY **term** FORMATTING):\n"
        "1. IN 'professional_summary' (CRITICAL): You MUST wrap 3 to 4 core platforms matching this specific Job Description in markdown asterisks (e.g., '...pipelines using **PySpark**, **Apache Kafka**, and **Google Cloud Platform** (GCP), orchestrated via **Apache Airflow**...'). NEVER return a summary without asterisks.\n"
        "2. IN 'experience_bullets': In EVERY bullet, wrap the primary engineering technique/platform (e.g., **Adaptive Query Execution**, **Change Data Capture (CDC)**, **Kafka**) AND the primary metric (e.g., **35%**, **99.9%**) in asterisks.\n"
        "3. DO NOT bold ubiquitous generic words like 'data', 'cloud', or 'engineered'. Bold only the standout skill and the metric."
    )

    phase_weighting_policy = (
        "1. EVALUATE TARGET EMPHASIS: Inspect the Job Description for explicit demand for BI, dashboards, or analytics reporting (e.g., Tableau, Power BI, Qlik, Looker, SSRS, 'Analytics Engineer', 'Business Intelligence').\n"
        "2. BACKEND / CORE DATA ENGINEERING ROLES (Default):\n"
        "   - If the JD does NOT explicitly demand BI/reporting platforms, ALLOCATE 85%+ of bullets to Phase 1 (Ingestion & Streaming), Phase 2 (Lakehouse & Storage), and Phase 3 (Distributed Transformations, Spark tuning, and Data Modeling).\n"
        "   - STRICTLY SUPPRESS Phase 5 (BI & Dashboards): Do NOT generate bullets detailing dashboard creation, business reports, or analyst support. Instead, frame data serving around partition-pruned SQL views, analytical schema optimization, and API delivery.\n"
        "   - BANNED VOCABULARY FOR CORE DE: Avoid terms like 'KPI tracking', 'executive dashboards', 'reports', or 'business analysts' unless specifically requested by the JD. Emphasize 'throughput', 'data integrity', 'cluster optimization', 'SLA adherence', and 'latency'.\n"
        "3. ANALYTICS & INSIGHTS ROLES:\n"
        "   - Only if the JD explicitly targets analytics/BI leadership, balance bullets between Phase 3 (Transformations/Modeling) and Phase 5 (Semantic Layers, BI Platforms, and Stakeholder Adoption).\n"
        "   - Populate BI tools in the skills block ONLY when requested."
    )

    return {
        "job_metadata": {
            "job_id": job.get("job_id"),
            "title": job.get("title"),
            "company": job.get("company"),
            "target_category": role_config.get("role_title", "Data Engineer"),
            "location": job.get("location") or "United States",
            "is_remote": job.get("is_remote", True),
            "job_url": job.get("job_url"),
            "priority_tech_stack": target_tech_stack,
            "tailoring_signals": target_signals,
        },
        "job_description": job.get("description"),
        "base_resume": base_resume,
        "tailoring_guidelines": {
            "core_architectural_boundaries": role_config.get("boundaries", ""),
            "static_bridging_rules": role_config.get("bridging_rules", ""),
            "domain_adaptation": "Adapt terminology to target employer (e.g., High Volume = self-healing, low-latency streaming; Financial = audit trails, KMS).",
            "tenure_enforcement": "The candidate has 8+ years of cumulative professional experience. The summary MUST explicitly open with this.",
            "bullet_distribution": {
                "job1_herc_rentals": 8,
                "job2_blue_yonder": 8,
                "job3_accenture": 9,
                "job4_thomson_reuters": 8,
            },
            "formula": "Google XYZ (Accomplished [X] as measured by [Y], by doing [Z]). Each bullet must address a distinct responsibility.",
            "skills_schema": role_config.get("skills_schema", {}),
            "formatting_rules": formatting_rules,
            "dynamic_phase_weighting_policy": phase_weighting_policy,
            "dynamic_tool_bridging_policy": {
                "instruction": (
                    "1. Extract required tools from the Job Description missing from the base resume.\n"
                    "2. Evaluate the functional category of the missing tool (e.g., Kafka = Streaming, dbt = Modeling/Transformation, Atlan = Governance).\n"
                    "3. Look up the corresponding phase in 'company_architectural_frameworks' below.\n"
                    "4. Conceptually replace the candidate's native tool with the JD's required tool strictly within that daily activity phase to generate the bullet.\n"
                    "5. Do NOT invent new architectural phases. If a tool contradicts the framework (e.g., frontend frameworks for data platform roles), ignore it."
                ),
                "company_architectural_frameworks": company_frameworks,
            },
        },
        "next_action": "Execute the 'export_tailored_resume' tool using the tailored content, then return the Google Doc URL, a Cover Letter (<300 words), and a Recruiter Outreach message.",
    }


prepare_job_tailoring_prompt = build_tailoring_prompt
build_job_tailoring_prompt = build_tailoring_prompt
