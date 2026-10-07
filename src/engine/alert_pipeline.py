# src/engine/alert_pipeline.py
import hashlib
import json
import logging
from typing import Any, Dict, Optional

from src.core.database import get_db_connection
from src.database.helper_repo import get_or_create_helper, log_outreach_event
from src.engine.email_analyzer import parse_whatsapp_alert_metadata
from src.engine.framework_selector import load_framework, select_slot_frameworks
from src.engine.llm_text import generate_text
from src.engine.tailor import export_tailored_resume_to_drive
from src.synthesis.gmail_client import create_gmail_draft
from src.synthesis.prompt_builder import (
    build_job_tailoring_prompt,
    build_whatsapp_outreach_prompt,
)

logger = logging.getLogger("de-job-intelligence.engine")

DEFAULT_MASTER_RESUME_URL = (
    "https://docs.google.com/document/d/1ODeobXRFlOpv3-SS__v4fh7gNZUJ1bN5lx2AhTQpD_pg/edit"
)


def _persist_whatsapp_job(
    meta: Dict[str, Any],
    whatsapp_text: str,
    company_name: str,
    job_title: str,
    extracted_jd: str,
) -> str:
    """Upserts the WhatsApp-sourced job into saved_jobs (source=whatsapp).

    Returns the job_id (wa-<hash>). Uses ON CONFLICT DO NOTHING so a repeated
    alert never clobbers an already-evaluated row.
    """
    digest = hashlib.sha1(whatsapp_text.encode("utf-8")).hexdigest()[:12]
    job_id = f"wa-{digest}"
    metadata = {
        "source": "whatsapp",
        "whatsapp_raw": whatsapp_text[:2000],
        "has_jd": meta.get("has_jd", False),
        "short_id": meta.get("short_id"),
        "recipient_email": meta.get("recipient_email"),
    }
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO saved_jobs (
                job_id, job_url, status, title, company, location, is_remote,
                salary_min, salary_max, fit_score, notes, description, metadata,
                employment_type, sponsorship, job_category, ai_status, saved_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, 'PENDING', CURRENT_TIMESTAMP
            )
            ON CONFLICT (job_id) DO NOTHING;
            """,
            (
                job_id, "", "saved", job_title, company_name, "United States", True,
                None, None, "0", "WhatsApp alert — pending AI evaluation.",
                extracted_jd, json.dumps(metadata),
                "Unknown", "Not Mentioned", "data_engineering",
            ),
        )
        conn.commit()
    return job_id


def _generate_tailored_content(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Runs the LLM over the tailoring bundle to produce the resume payload."""
    guidelines = bundle["tailoring_guidelines"]
    slot_fws = guidelines["dynamic_tool_bridging_policy"]["slot_frameworks"]
    slot_headers = {
        slot: data.get("resume_slot", {}).get("header", slot)
        for slot, data in slot_fws.items()
    }
    prompt = f"""You are tailoring a resume for a specific job. Follow ALL guidelines exactly.

BASE RESUME:
{bundle['base_resume'][:6000]}

JOB TITLE: {bundle['job_metadata']['title']} at {bundle['job_metadata']['company']}
JOB DESCRIPTION:
{(bundle['job_description'] or '')[:4000]}

SLOT FRAMEWORKS (JSON — each slot has its own framework, bullet_bank, and verified resume_slot header):
{json.dumps(slot_fws, indent=2)[:8000]}

GUIDELINES:
- Core boundaries: {guidelines['core_architectural_boundaries']}
- {guidelines['bullet_selection_contract']}
- {guidelines['fidelity_rules']}
- Formatting: {guidelines['formatting_rules']}
- Bullet distribution per slot: {json.dumps(guidelines['bullet_distribution'])}

Return a STRICT JSON object (no markdown, no backticks) with EXACTLY these keys:
- "professional_summary": string
- "technical_skills": object mapping skill-group name -> comma-separated skills string
- "experience_bullets": object with EXACTLY these keys -> list of bullet strings: {", ".join(guidelines['bullet_distribution'].keys())}
- "slot_headers": use these EXACT employer headers per slot: {json.dumps(slot_headers)}
"""
    raw = generate_text(prompt, json_mode=True, temperature=0.2, task="whatsapp-tailor-resume")
    return json.loads(raw)


def process_whatsapp_job_alert(
    whatsapp_text: str,
    helper_name: str,
    helper_email: Optional[str] = None,
    helper_company: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Optimized WhatsApp job alert pipeline:
    1. Parses metadata, email handles, and extracts recruiter name & domain short ID.
    2. Conditional RAG tailoring vs Quota Saver default resume.
    3. Generates precise template-matched email draft with helper in CC.
    4. Logs everything into database including the domain short_id.
    """
    logger.info(f"🚀 Processing WhatsApp alert via helper: {helper_name}")

    # 1. Resolve or register helper
    helper_id = get_or_create_helper(
        name=helper_name,
        email=helper_email,
        company=helper_company,
        metadata={"source": "whatsapp_alert_pipeline"},
    )

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT email, company FROM scout.helpers WHERE helper_id = %s;", (helper_id,))
        h_row = cur.fetchone()
        resolved_helper_email = h_row["email"]

    # 2. Parse metadata (including recruiter name, recipient email, and short_id)
    meta = parse_whatsapp_alert_metadata(whatsapp_text)
    has_jd = meta.get("has_jd", False)
    company_name = meta.get("company_name", "Direct Client")
    job_title = meta.get("job_title", "Data Engineer")
    recipient_email = meta.get("recipient_email")
    recruiter_name = meta.get("recruiter_name")
    extracted_jd = meta.get("extracted_jd", whatsapp_text)
    short_id = meta.get("short_id")  # <--- Domain-derived 4-char short ID

    logger.info(
        f"🏢 Company: {company_name} | Role: {job_title} | Recruiter: {recruiter_name} | Short ID: {short_id} | Has JD: {has_jd}"
    )

    # 3. Persist WhatsApp job + ONE shared slot decision (reused for
    #    both the resume and the outreach email below). Slot 1 switches on
    #    the JD (healthcare -> optum, else herc_rentals); slots 2-4 fixed.
    job_id = _persist_whatsapp_job(meta, whatsapp_text, company_name, job_title, extracted_jd)
    slot_frameworks = select_slot_frameworks(extracted_jd, job_title)
    lead_fw = load_framework(slot_frameworks["job1"])
    framework_context = (
        f"{slot_frameworks['job1']}: {lead_fw.get('summary_angle', '')} "
        f"({lead_fw.get('domain', '')})"
    )
    logger.info(
        "🧭 Slots: %s | job_id=%s",
        {s: slot_frameworks[s] for s in ("job1", "job2", "job3", "job4")},
        job_id,
    )

    # 4. Real framework-aware tailoring vs Quota Saver
    resume_url = DEFAULT_MASTER_RESUME_URL
    if has_jd:
        try:
            logger.info("✨ Rich JD detected: running framework-aware tailoring...")
            bundle = build_job_tailoring_prompt(job_id, slot_frameworks=slot_frameworks)
            if "error" in bundle:
                raise RuntimeError(bundle["error"])
            tailored = _generate_tailored_content(bundle)
            export_result = export_tailored_resume_to_drive(
                {
                    **tailored,
                    "company_name": company_name,
                    "job_title": job_title,
                    "slot_frameworks": slot_frameworks,
                }
            )
            if export_result.get("status") == "success":
                resume_url = export_result["document_url"]
                logger.info(f"📄 Tailored resume: {resume_url}")
            else:
                raise RuntimeError(export_result.get("message"))
        except Exception as e:
            logger.warning(f"Tailoring failed ({e}); falling back to master resume.")
            resume_url = DEFAULT_MASTER_RESUME_URL
    else:
        logger.info("⚡ Lightweight alert detected: Using default master resume to save quota.")

    # 5. Build framework-aware email prompt using extracted recruiter name
    email_prompt = build_whatsapp_outreach_prompt(
        company_name=company_name,
        job_title=job_title,
        extracted_jd=extracted_jd,
        helper_name=helper_name,
        resume_url=resume_url,
        recruiter_name=recruiter_name,
        framework_context=framework_context,
    )

    try:
        # Config-driven failover chain (config/llm.yaml): tries providers in
        # order, sidelining any that hit quota/errors, with WhatsApp alerts.
        email_json = generate_text(
            email_prompt,
            json_mode=True,
            temperature=0.2,
            task="whatsapp-outreach-email",
        )
        email_data = json.loads(email_json)
        subject = email_data.get(
            "subject", f"Application for {job_title} – {company_name} – Sri Omkar D"
        )
        body = email_data.get(
            "body",
            f"Hi {recruiter_name or 'Hiring Team'},\n\nI'm interested in the {job_title} role. My resume is here:\n{resume_url}\n\nBest,\nSri Omkar D",
        )
    except Exception as e:
        logger.error(f"Failed to generate dynamic email: {e}")
        subject = f"Application for {job_title} – {company_name} – Sri Omkar D"
        body = (
            f"Hi {recruiter_name or 'Hiring Team'},\n\n"
            f"I am writing to express my interest in the {job_title} position. "
            f"You can review my background here:\n{resume_url}\n\nBest regards,\nSri Omkar D"
        )

    # 5. Create Gmail Draft (Helper in CC, absent from body)
    draft_result = create_gmail_draft(
        helper_email=resolved_helper_email,
        helper_name=helper_name,
        company_name=company_name,
        job_title=job_title,
        resume_url=resume_url,
        recipient_email=recipient_email,
        dynamic_email_content={"subject": subject, "body": body},
    )

    # 6. Log Audit Record in PostgreSQL with short_id
    outreach_id = log_outreach_event(
        helper_id=helper_id,
        company_name=company_name,
        job_title=job_title,
        extracted_jd=extracted_jd,
        resume_doc_url=resume_url,
        gmail_draft_id=draft_result["draft_id"],
        recruiter_name=recruiter_name,
        short_id=short_id,
        metadata={
            "whatsapp_raw": whatsapp_text,
            "has_jd": has_jd,
            "recipient_email": recipient_email,
            "subject": subject,
            "cc_helper": resolved_helper_email,
            "short_id": short_id,
            "job_id": job_id,
            "slot_frameworks": slot_frameworks,
        },
    )

    logger.info(
        f"✨ Successfully processed alert! Outreach DB ID: {outreach_id} | Domain Code: {short_id}"
    )

    return {
        "status": "success",
        "outreach_id": outreach_id,
        "short_id": short_id,
        "job_id": job_id,
        "company_name": company_name,
        "job_title": job_title,
        "recruiter_name": recruiter_name,
        "has_jd": has_jd,
        "framework_selection": slot_frameworks,
        "resume_url": resume_url,
        "gmail_draft_id": draft_result["draft_id"],
    }
