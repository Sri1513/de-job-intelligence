# src/engine/alert_pipeline.py
import json
import logging
from typing import Any, Dict, Optional

from google import genai
from google.genai import types

from src.core.config import settings
from src.core.database import get_db_connection
from src.database.helper_repo import get_or_create_helper, log_outreach_event
from src.engine.email_analyzer import parse_whatsapp_alert_metadata
from src.synthesis.gdrive_docs import generate_resume_from_llm_payload
from src.synthesis.gmail_client import create_gmail_draft
from src.synthesis.prompt_builder import build_whatsapp_outreach_prompt

logger = logging.getLogger("de-job-intelligence.engine")

# Lazily-initialized google-genai client. Created on first use, never at import,
# so importing this module does not require an API key to be configured.
_genai_client = None


def get_genai_client():
    """Returns the shared genai client, creating it on first use."""
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _genai_client

DEFAULT_MASTER_RESUME_URL = (
    "https://docs.google.com/document/d/1ODeobXRFlOpv3-SS__v4fh7gNZUJ1bN5lx2AhTQpD_pg/edit"
)


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

    # 3. Conditional Resume Tailoring vs Quota Saver
    if has_jd:
        logger.info("✨ Rich JD detected: Triggering custom RAG resume tailoring pipeline...")
        llm_payload = {
            "professional_summary": (
                "Senior Data Engineer with 7+ years of experience designing scalable "
                "data platforms and lakehouse architectures across AWS and PySpark."
            ),
            "technical_skills": {
                "bigdata": "Apache Spark, PySpark, Spark SQL, Databricks, Delta Lake",
                "languages": "Python, SQL, Bash",
                "devops": "Apache Airflow, Docker, Git, CI/CD",
                "databases": "Snowflake, PostgreSQL, Amazon Redshift",
            },
            "experience_bullets": {},
        }
        document_title = f"{company_name} - Tailored Resume - Data Engineer"
        resume_url = generate_resume_from_llm_payload(llm_payload, document_title=document_title)
    else:
        logger.info("⚡ Lightweight alert detected: Using default master resume to save quota.")
        resume_url = DEFAULT_MASTER_RESUME_URL

    # 4. Build email prompt using extracted recruiter name
    email_prompt = build_whatsapp_outreach_prompt(
        company_name=company_name,
        job_title=job_title,
        extracted_jd=extracted_jd,
        helper_name=helper_name,
        resume_url=resume_url,
        recruiter_name=recruiter_name,
    )

    try:
        response = get_genai_client().models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=email_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        email_data = json.loads(response.text)
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
        },
    )

    logger.info(
        f"✨ Successfully processed alert! Outreach DB ID: {outreach_id} | Domain Code: {short_id}"
    )

    return {
        "status": "success",
        "outreach_id": outreach_id,
        "short_id": short_id,
        "company_name": company_name,
        "job_title": job_title,
        "recruiter_name": recruiter_name,
        "has_jd": has_jd,
        "resume_url": resume_url,
        "gmail_draft_id": draft_result["draft_id"],
    }
