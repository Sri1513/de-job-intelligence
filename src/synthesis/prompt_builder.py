# src/synthesis/prompt_builder.py

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from src.core.config import settings
from src.core.database import get_db_connection
from src.engine.framework_selector import (
    load_framework,
    select_slot_frameworks,
)

logger = logging.getLogger(__name__)

CONFIG_DIR = getattr(settings, "CONFIG_DIR", Path(__file__).resolve().parents[2] / "config")

_RESUME_CACHE: dict[str, str] = {}
_ROLE_CACHE: dict[str, dict] = {}
_FRAMEWORK_CACHE: dict[str, dict] = {}

# Resume job slots. Keys are generic (job1..job4); each slot's employer
# header comes from its framework's resume_slot block — never invented.
BULLET_DISTRIBUTION = {
    "job1": 8,
    "job2": 8,
    "job3": 9,
    "job4": 8,
}

# Tool translation: the ETL workflow is the same, only the tools vary.
# When the JD names a tool with no bank coverage, the engine highlights the
# candidate's REAL equivalent skill instead of omitting or fabricating.
TOOL_TRANSLATION_POLICY = (
    "TOOL TRANSLATION (MANDATORY — the workflow is the same, only the tools vary):\n"
    "1. When the JD names a tool with no bank coverage, look it up in 'skill_equivalences'.\n"
    "2. Highlight the candidate's EQUIVALENT real skill as the answer — e.g. JD asks 'Scala' → "
    "the resume showcases **PySpark / Apache Spark**, because Spark is the engine and PySpark is the "
    "candidate's interface to it. Same for warehousing (JD 'BigQuery' → Redshift) and ELT (JD 'Matillion' → dbt/Glue).\n"
    "3. The JD's keyword appears in Technical Skills for ATS, always paired with the real equivalent "
    "(e.g. 'Scala (via PySpark / Apache Spark)') — never as a standalone claimed proficiency.\n"
    "4. Experience bullets describe ONLY the real equivalent work. Never write 'built X in Scala' when the "
    "work was PySpark. The bullet proves the equivalent; the keyword match lives in the skills matrix.\n"
    "5. CONFIDENCE: each mapping is tagged green (same engine/workflow — pair freely) or yellow (conceptual "
    "relative — pair only when the JD leans on it and no green mapping covers it). Never stretch a yellow "
    "mapping into a claim of hands-on experience.\n"
)

# Identity fidelity: facts that must be copied EXACTLY from the base resume.
FIDELITY_RULES = (
    "IDENTITY FIDELITY RULES (MANDATORY — violating these is a failure):\n"
    "1. Candidate name is 'Sri Omkar D' — never expand, alter, or initial differently.\n"
    "2. Employer header, job title, location, dates for EACH slot: use that slot's "
    "framework 'resume_slot' block EXACTLY (it carries the verified headers). "
    "Never substitute one employer for another, never invent headers.\n"
    "3. Education: degree names, schools, years EXACTLY as in BASE RESUME.\n"
    "4. Technical Skills: lead with the candidate's REAL tools from BASE RESUME. A JD-requested tool "
    "with no resume coverage still appears via TOOL TRANSLATION — the JD keyword is listed for ATS, always "
    "paired with the equivalent real skill, never as a standalone proficiency.\n"
    "5. Every experience bullet MUST be traceable to the selected framework's bullet_bank. "
    "If a JD requirement matches nothing in the bank, TRANSLATE it: consult 'skill_equivalences' and select "
    "the bullet proving the equivalent REAL skill (e.g. JD 'Scala' → the PySpark/Spark bullet). Never claim "
    "hands-on experience with the JD's tool itself.\n"
    "6. No duplicate bullets. Bold whole terms only (**Redshift**), never bare numbers "
    "mid-sentence."
)

# Plain-voice + honesty rules for recruiter outreach.
EMAIL_VOICE_RULES = (
    "VOICE AND HONESTY RULES (MANDATORY):\n"
    "- Write plain and direct, like the candidate writes. NEVER use: leveraged, robust, "
    "seamless, rigorous, cutting-edge, state-of-the-art, or 'guarantee 100%'.\n"
    "- Every alignment bullet must reflect techniques/tools actually on the resume. Do NOT "
    "invent techniques (e.g. tokenization) the resume never mentions. If the JD names "
    "something the resume lacks, describe the adjacent real experience instead of "
    "claiming the missing one.\n"
    "- No absolute guarantees. State what was done and measured."
)


def build_whatsapp_outreach_prompt(
    company_name: str,
    job_title: str,
    extracted_jd: str,
    helper_name: str,
    resume_url: str,
    recruiter_name: str = None,
    framework_context: str = "",
) -> str:
    """
    Builds a precise, structured prompt for Gemini to draft a human-like email
    matching Sri Omkar's exact format from the reference screenshot.

    framework_context: one-to-two line description of the selected resume
    framework (e.g. "optum: Healthcare legacy ETL migration ..."). The
    alignment bullets are steered to reflect the framework's angles so the
    email and the tailored resume tell the same story.
    """
    salutation_target = f"Hi {recruiter_name}," if recruiter_name else "Hi Hiring Team,"

    framework_block = (
        f"\n    - Selected Resume Framework: {framework_context}\n"
        "    - FRAMEWORK ALIGNMENT: the 4 alignment bullets below MUST reflect the "
        "selected framework's angles (e.g. a healthcare-migration framework -> "
        "parity/UAT/governance bullets; a modern-stack framework -> Spark/Airflow "
        "tuning bullets)."
        if framework_context
        else ""
    )

    return f"""
    You are Sri Omkar D, a Senior Data Engineer with 7+ years of experience specializing in PySpark, AWS, distributed lakehouses, and Apache Airflow.
    
    TARGET CONTEXT:
    - Company: {company_name}
    - Job Title: {job_title}
    - Recruiter First Name: {recruiter_name or "Unknown"}
    - Details/Requirements: {extracted_jd}
    - Resume Link: {resume_url}{framework_block}

    INSTRUCTIONS FOR EMAIL GENERATION:
    You must structure the email to strictly match Sri Omkar's proven high-converting outreach format:

    1. SUBJECT LINE:
       Format: "Application for {job_title} – {company_name} – Sri Omkar D"

    2. EMAIL BODY:
       - SALUTATION: "{salutation_target}"
       - OPENING: "I hope you are having a great day." followed by "I am reaching out regarding the {job_title} position at {company_name}. With over 7 years of hands-on data engineering experience designing, building, and optimizing scalable batch and near-real-time data pipelines across cloud platforms, I am confident in my ability to deliver immediate value to your client's team."
       - ALIGNMENT BULLETS SECTION: Include the exact header line: 
         "A summary of how my technical background aligns with your core requirements:"
         Followed by 4 bullet points (using bullet character '• ') with bold subheadings before colons (e.g., "• ETL/ELT & Distributed Processing: ...").
       - CANDIDATE SUMMARY BLOCK: Include the exact header line:
         "Candidate Summary:"
         Followed by bullet points (using bullet character '• ') with bold keys before colons:
         • Total Experience: 7+ Years
         • Work Authorization: F1 VISA (STEM OPT - EAD) – H-1B picked, petition filed
         • Role Focus: Data Engineer / Senior Data Engineer
         • LinkedIn: linkedin.com/in/sri-omkar-58r4r4r8
         • Portfolio: www.sriomkar.com
       - CLOSING: "I have attached my detailed resume for your review. I would welcome the opportunity to connect for a brief introductory call to discuss the project details and next steps."
       - SIGN-OFF: 
         Best regards,
         Sri Omkar D,
         +1 (951) 545-2146 | sri.omkar.d@gmail.com
         sriomkar.com | linkedin.com/in/sri-omkar-58r4r4r8

    - **CRITICAL RULE**: DO NOT mention the helper ({helper_name}) or any referral source in the email body AT ALL. (They are placed in the CC field automatically).

    {EMAIL_VOICE_RULES}

    Return a JSON object with EXACTLY these two keys:
    - "subject": (string)
    - "body": (string - formatted with proper line breaks matching the layout above)
    """


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
                "Senior Data Engineer with 7+ years of experience across Apache Spark, PySpark, "
                "Python, SQL, AWS, Azure, Snowflake, Databricks, and Lakehouse architectures."
            )

    return _RESUME_CACHE[resume_file_suffix]


_EQUIV_CACHE: dict | None = None


def load_skill_equivalences() -> dict:
    """Load the JD-tool -> candidate-equivalent-skill map (v1). Cached."""
    global _EQUIV_CACHE
    if _EQUIV_CACHE is None:
        eq_file = CONFIG_DIR / "skill_equivalences.json"
        try:
            with open(eq_file, encoding="utf-8") as f:
                _EQUIV_CACHE = json.load(f)
        except (OSError, json.JSONDecodeError):
            _EQUIV_CACHE = {"version": 0, "equivalences": {}}
    return _EQUIV_CACHE


def build_job_tailoring_prompt(
    job_id: str,
    slot_frameworks: dict | None = None,
    job_override: dict | None = None,
) -> Dict[str, Any]:
    """Builds the tailoring prompt bundle for a job.

    slot_frameworks: optional pre-computed SLOT map from
    src.engine.framework_selector.select_slot_frameworks (shared by the
    WhatsApp path so one decision drives resume + email). When omitted,
    the selector runs here. Slot 1 switches on the JD (healthcare -> optum,
    else herc_rentals); slots 2-4 are fixed.

    job_override: optional pre-parsed job dict (same shape as a saved_jobs
    row). When given, the saved_jobs DB lookup is skipped — used by the
    WhatsApp path, whose leads live in scout.whatsapp_messages, not
    saved_jobs.
    """
    if job_override is not None:
        job = job_override
    else:
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
        "devops",
        "cloud",
        "infrastructure",
        "platform",
        "sre",
        "reliability",
        "kubernetes",
        "terraform",
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
    base_resume = load_target_resume(role_slug)

    # Per-slot framework routing (one shared decision): slot 1 switches on the JD
    # (healthcare -> optum, else herc_rentals); slots 2-4 are fixed.
    # Callers may pass a pre-computed slot map (WhatsApp path) so one decision
    # drives resume + email.
    if slot_frameworks is None:
        slot_frameworks = select_slot_frameworks(
            job.get("description") or "", job.get("title") or ""
        )
    slot_ids = {s: slot_frameworks[s] for s in ("job1", "job2", "job3", "job4")}
    slot_data: dict[str, dict] = {}
    for slot, fw_id in slot_ids.items():
        fw = load_framework(fw_id)
        slot_data[slot] = {
            "framework_id": fw_id,
            "resume_slot": fw.get("resume_slot", {}),
            "summary_angle": fw.get("summary_angle", ""),
            "domain": fw.get("domain", ""),
            "bullet_bank": fw.get("bullet_bank", []),
            "process_framework": fw.get("process_framework", {}),
        }
    logger.info(
        "tailoring: job %s -> slots %s",
        job.get("job_id"),
        {s: slot_data[s]["framework_id"] for s in slot_data},
    )

    # Deterministic bullet ranking (Perfect Resume Engine, Phase 2):
    # each slot's bank pre-ranked by weighted canonical-skill overlap with
    # the JD, so the LLM selects from computed scores instead of vibes.
    from src.engine.bullet_ranker import rank_all_slots

    jd_text = job.get("description") or ""
    ranked_banks = rank_all_slots(slot_data, jd_text)
    deterministic_ranking = {
        slot: [
            {"id": b.get("id"), "score": b["_overlap_score"],
             "matched": b["_matched_skills"]}
            for b in ranked[:8]
        ]
        for slot, ranked in ranked_banks.items()
    }

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
        "slot_frameworks": slot_ids,
        "lead_framework_id": slot_ids["job1"],
        "tailoring_guidelines": {
            "core_architectural_boundaries": role_config.get("boundaries", ""),
            "static_bridging_rules": role_config.get("bridging_rules", ""),
            "domain_adaptation": "Adapt terminology to target employer (e.g., High Volume = self-healing, low-latency streaming; Financial = audit trails, KMS).",
            "tenure_enforcement": "The candidate has 7+ years of cumulative professional experience. The summary MUST explicitly open with this.",
            "bullet_distribution": BULLET_DISTRIBUTION,
            "deterministic_bullet_ranking": deterministic_ranking,
            "fidelity_rules": FIDELITY_RULES,
            "tool_translation_policy": TOOL_TRANSLATION_POLICY,
            "skill_equivalences": load_skill_equivalences().get("equivalences", {}),
            "formula": "Google XYZ (Accomplished [X] as measured by [Y], by doing [Z]). Each bullet must address a distinct responsibility.",
            "skills_schema": role_config.get("skills_schema", {}),
            "formatting_rules": formatting_rules,
            "dynamic_phase_weighting_policy": phase_weighting_policy,
            "dynamic_tool_bridging_policy": {
                "instruction": (
                    "Per slot (job1..job4), each with its own framework in 'slot_frameworks' below:\n"
                    "1. Extract required tools from the Job Description missing from the base resume.\n"
                    "2. Evaluate the functional category of the missing tool (e.g., Kafka = Streaming, dbt = Modeling/Transformation, Atlan = Governance).\n"
                    "3. Look up the corresponding phase in THAT SLOT's framework.\n"
                    "4. The JD's requested tools MUST appear: swap the JD's tool name into the bullet's 'bridging_slots' so JD keywords appear verbatim — but ONLY within the same phase family.\n"
                    "5. Do NOT invent new architectural phases. If a tool contradicts the slot's framework (e.g., frontend frameworks for data platform roles), ignore it — believability over keyword stuffing."
                ),
                "slot_frameworks": slot_data,
            },
            "bullet_selection_contract": (
                "BULLET SELECTION CONTRACT (MANDATORY — you are a SELECTOR, not an inventor):\n"
                "Work EACH slot independently (job1..job4), using ONLY that slot's 'bullet_bank'. \n"
                "1. EXTRACT the JD's required skills, responsibilities, and domain signals.\n"
                "2. RANK that slot's bank entries by JD keyword overlap (skills first, then signals/phases) — "
                "points must be chosen by what the JD asks for, per that slot's company framework. "
                "A deterministic pre-ranking is provided in 'deterministic_bullet_ranking' below (computed "
                "from exact canonical-skill overlap with the JD, highest score first) — PREFER this order; "
                "it is ground truth for skill coverage, not a suggestion.\n"
                "3. SELECT the top bullets per 'bullet_distribution' for the slot; the set must cover the JD's top "
                "requirements with no two bullets proving the same thing.\n"
                "3b. JD-CONDITIONAL TOOLS: a bank bullet for a tool the JD never mentions (e.g. dbt when the JD doesn't ask for dbt) may only fill remaining slots after all JD-covering bullets are placed — never as a lead bullet, never in the summary's tool line. The resume mirrors the JD; it doesn't advertise off-JD tools.\n"
                "3c. SLOT FIDELITY: each slot draws bullets ONLY from its own framework's bullet bank in 'slot_frameworks'. Never borrow a bullet from another slot's framework \u2014 a HIPAA bullet from the Optum bank does not belong in the Herc Rentals slot, no matter how well it reads.\n"
                "4. BRIDGE: where the JD names a tool in the same phase family (see the slot framework's "
                "'swappable_categories'), swap it into the bullet's 'bridging_slots' so the JD's tools appear "
                "verbatim. Keep the candidate's wording and metrics EXACT — never alter a number or invent a new one.\n"
                "5. FORBID: no new achievements, no new metrics, no invented responsibilities (leading teams, mentoring, defining standards — unless in the bank). "
                "Banned words: 'leveraging', 'leveraged', 'spearheaded', 'spearhead', 'mastered', 'passionate', 'cutting-edge'. "
                "Banned absolutes: 'zero data loss', '0% data loss', 'zero downtime', '100%', 'guarantee'.\n"
                "6. SUMMARY: write the professional summary FRESH for this JD as EXACTLY 6 lines covering the WHOLE career (all four slots), never copied from anywhere. Output 6 plain lines, NO bullet characters (the template provides the bullets).\\n"
                "Line 1: '7+ years' as an ETL Data Engineer + the through-line across all roles (e.g. legacy-to-cloud migrations across healthcare, retail, finance).\\n"
                "Line 2: current/most recent role proof point — one verifiable metric from the lead slot's bank (never invented).\\n"
                "Line 3: earlier roles breadth — 1-2 verifiable proof points spanning the other slots.\\n"
                "Line 4: reliability/governance strength, verifiable from the banks.\\n"
                "Line 5: domain angle weighted to THIS JD (e.g. healthcare governance for a healthcare JD).\\n"
                "Line 6: tool coverage spanning the career, emphasizing THIS JD's top matched skills.\\n"
                "Plain direct voice, no AI polish ('utilizing', 'leveraging', 'passionate' are banned). Bold 1-2 key terms per line."
            ),
        },
        "next_action": "Execute the 'export_tailored_resume' tool using the tailored content, then return the Google Doc URL, a Cover Letter (<300 words), and a Recruiter Outreach message.",
    }


prepare_job_tailoring_prompt = build_job_tailoring_prompt
build_tailoring_prompt = build_job_tailoring_prompt
