# src/engine/prompt_builder.py
import json
from typing import Dict, Any
from src.core.config import settings
from src.core.database import get_job_by_id
from src.core.utils import get_cached_resume

def load_frameworks() -> dict:
    """Loads the deterministic RAG framework mapping for semantic tool swapping."""
    framework_file = settings.CONFIG_DIR / "frameworks" / "company_frameworks.json"
    if not framework_file.exists():
        raise FileNotFoundError(f"Framework mapping missing at {framework_file}")
    with open(framework_file, "r", encoding="utf-8") as f:
        return json.load(f)

def load_role_config(category_slug: str) -> dict:
    """Loads the role config mapping boundaries, bridging directives, and schema."""
    slug = "devops" if "devops" in category_slug.lower() else "data_engineering"
    config_file = settings.CONFIG_DIR / "roles" / f"{slug}.json"

    if not config_file.exists():
        raise FileNotFoundError(f"Role config missing at {config_file}")
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)

def build_tailoring_payload(job_id: str) -> Dict[str, Any]:
    """
    Constructs the secure JSON-structured tailoring payload with dynamic phase weighting
    and deterministic tool bridging policies.
    """
    job = get_job_by_id(job_id)
    if not job:
        return {"error": f"No job found in PostgreSQL for ID '{job_id}'."}

    category = (job.get("job_category") or "").lower()
    title_lower = (job.get("title") or "").lower()

    devops_keywords = ["devops", "cloud", "infrastructure", "platform", "sre", "reliability", "kubernetes", "terraform"]
    category_slug = "devops" if "devops" in category or any(kw in title_lower for kw in devops_keywords) else "data_engineering"

    config = load_role_config(category_slug)
    frameworks = load_frameworks()
    base_resume = get_cached_resume(category_slug)

    return {
        "job_metadata": {
            "job_id": job["job_id"],
            "title": job["title"],
            "company": job["company"],
            "target_category": config.get("role_title", "Data Engineer"),
            "location": job.get("location", "Unknown"),
            "is_remote": job.get("is_remote", False),
            "job_url": job.get("job_url", "")
        },
        "job_description": job.get("description", ""),
        "base_resume": base_resume,
        "tailoring_guidelines": {
            "core_architectural_boundaries": config.get("boundaries", ""),
            "static_bridging_rules": config.get("bridging_rules", ""),
            "domain_adaptation": "Adapt terminology to target employer (e.g., High Volume = low-latency streaming; Financial = audit trails, KMS).",
            "tenure_enforcement": "The candidate has 8+ years of cumulative professional experience. The summary MUST explicitly open with this.",
            "bullet_distribution": {
                "job1_herc_rentals": 8,
                "job2_blue_yonder": 8,
                "job3_accenture": 9,
                "job4_thomson_reuters": 8
            },
            "formula": "Google XYZ (Accomplished [X] as measured by [Y], by doing [Z]). Each bullet must address a distinct responsibility.",
            "skills_schema": config.get("skills_schema", {}),
            "formatting_rules": "Output plain text strings. Do NOT include markdown bolding (**) in the strings.",
            "dynamic_phase_weighting_policy": (
                "1. EVALUATE TARGET EMPHASIS: Inspect the Job Description for explicit demand for BI, dashboards, or analytics reporting "
                "(e.g., Tableau, Power BI, Qlik, Looker, SSRS, 'Analytics Engineer', 'Business Intelligence').\n"
                "2. BACKEND / CORE DATA ENGINEERING ROLES (Default):\n"
                "   - If the JD does NOT explicitly demand BI/reporting platforms, ALLOCATE 85%+ of bullets to Phase 1 (Ingestion & Streaming), "
                "Phase 2 (Lakehouse & Storage), and Phase 3 (Distributed Transformations, Spark tuning, and Data Modeling).\n"
                "   - STRICTLY SUPPRESS Phase 5 (BI & Dashboards): Do NOT generate bullets detailing dashboard creation, business reports, or "
                "analyst support. Instead, frame data serving around partition-pruned SQL views, analytical schema optimization, and API delivery.\n"
                "   - BANNED VOCABULARY FOR CORE DE: Avoid terms like 'KPI tracking', 'executive dashboards', 'reports', or 'business analysts' "
                "unless specifically requested by the JD. Emphasize 'throughput', 'data integrity', 'cluster optimization', 'SLA adherence', and 'latency'.\n"
                "3. ANALYTICS & INSIGHTS ROLES:\n"
                "   - Only if the JD explicitly targets analytics/BI leadership, balance bullets between Phase 3 (Transformations/Modeling) and "
                "Phase 5 (Semantic Layers, BI Platforms, and Stakeholder Adoption).\n"
                "   - Populate BI tools in the skills block ONLY when requested."
            ),
            "dynamic_tool_bridging_policy": {
                "instruction": (
                    "1. Extract required tools from the Job Description missing from the base resume.\n"
                    "2. Evaluate the functional category of the missing tool (e.g., Kafka = Streaming, dbt = Modeling, Atlan = Governance).\n"
                    "3. Look up the corresponding phase in 'company_architectural_frameworks' below.\n"
                    "4. Conceptually replace the candidate's native tool with the JD's required tool strictly within that daily activity phase.\n"
                    "5. Do NOT invent new architectural phases."
                ),
                "company_architectural_frameworks": frameworks
            }
        },
        "next_action": "Execute 'export_tailored_resume' using the tailored content, then return the Google Doc URL, Cover Letter (<300 words), and Recruiter Outreach message."
    }