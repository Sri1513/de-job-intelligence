# src/engine/email_analyzer.py
import json
import logging
import warnings
from typing import Any, Dict

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import google.generativeai as genai

from src.core.config import settings
from src.engine.pipeline_utils import generate_domain_short_id  # <--- Imported from pipeline_utils

logger = logging.getLogger("de-job-intelligence.engine")
genai.configure(api_key=settings.GEMINI_API_KEY)

def parse_whatsapp_alert_metadata(whatsapp_text: str) -> Dict[str, Any]:
    """
    Parses a raw WhatsApp message to extract company, role, recipient email,
    clean recruiter first name, whether a Job Description is present, 
    and appends the domain-derived short ID.
    """
    prompt = f"""
    Analyze the following raw WhatsApp job alert message:
    {whatsapp_text}

    INSTRUCTIONS:
    1. "has_jd": (boolean) Set to true ONLY IF the message contains actual responsibilities, technical requirements, or a detailed job description. Set to false if it is just a short notice with an email.
    2. "company_name": Extract company/agency name if present, infer from email domain if possible (e.g. 'valuetechinc.com' -> 'Value Tech'), or fallback to 'Direct Client'.
    3. "job_title": Extract role title (default to 'Data Engineer').
    4. "recipient_email": Extract the application email address if present (or null).
    5. "recruiter_name": Extract ONLY the recruiter's FIRST name (e.g., if text says 'Neha Mishra' or email is 'Mishra.Neha@...', extract just 'Neha'; if 'Roshini D', extract just 'Roshini'). Return null if none found.
    6. "extracted_jd": The cleaned text or requirements summary.

    Return a JSON object with EXACTLY these keys:
    - "has_jd": bool
    - "company_name": string
    - "job_title": string
    - "recipient_email": string or null
    - "recruiter_name": string or null
    - "extracted_jd": string
    """

    try:
        model = genai.GenerativeModel(
            model_name=settings.GEMINI_MODEL,
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0.1,
            }
        )
        response = model.generate_content(prompt)
        data = json.loads(response.text)
        
        # Python safeguard: ensure recruiter_name is strictly the first name
        raw_name = data.get("recruiter_name")
        if raw_name:
            clean_first = raw_name.strip().split()[0].replace(".", "")
            data["recruiter_name"] = clean_first

        # Automatically call the imported short ID generator using the email domain
        data["short_id"] = generate_domain_short_id(
            data.get("recipient_email"), 
            data.get("company_name")
        )

        return data
    except Exception as e:
        logger.error(f"Failed to parse WhatsApp metadata: {e}")
        fallback_company = "Direct Client"
        return {
            "has_jd": False,
            "company_name": fallback_company,
            "job_title": "Data Engineer",
            "recipient_email": None,
            "recruiter_name": None,
            "extracted_jd": whatsapp_text,
            "short_id": generate_domain_short_id(None, fallback_company)
        }
