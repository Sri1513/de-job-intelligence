# src/engine/analyzer.py
import json
import logging
from typing import Any

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

import google.generativeai as genai

from src.core.config import settings
from src.core.utils import get_cached_resume

logger = logging.getLogger("de-job-intelligence.analyzer")

def get_gemini_model():
    """Initializes and configures the Gemini generative model."""
    if not settings.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured in settings or .env file.")
    genai.configure(api_key=settings.GEMINI_API_KEY)
    return genai.GenerativeModel("gemini-3.6-flash")

ANALYSIS_SYSTEM_PROMPT = """
You are an expert technical recruiter and Senior Data Engineering evaluation agent.
Analyze the following Job Description against the Candidate's Master Profile.

Evaluate:
1. match_score (integer between 0 and 100) based strictly on tech stack alignment.
2. key_matches (array of matching technologies).
3. missing_skills (array of required technologies the candidate lacks).
4. role_focus ('CORE_DE', 'ANALYTICS_ENGINEERING', or 'DEVOPS_PLATFORM').
5. summary_rationale (concise 2-sentence summary of candidate fit).

Output MUST be strict JSON matching this schema:
{
  "match_score": 85,
  "key_matches": ["Apache Spark", "Python", "Snowflake"],
  "missing_skills": ["Kafka"],
  "role_focus": "CORE_DE",
  "summary_rationale": "Strong fit for distributed compute requirements."
}
Do not wrap output in markdown codeblocks. Return valid JSON only.
"""

def evaluate_job_fit(job_description: str, category_slug: str = "data_engineering") -> dict[str, Any]:
    """
    Invokes Gemini to evaluate job requirements against candidate experience.
    """
    if not job_description or len(job_description.strip()) < 50:
        return {
            "match_score": 0,
            "key_matches": [],
            "missing_skills": [],
            "role_focus": "UNKNOWN",
            "summary_rationale": "Job description insufficient for analysis."
        }

    resume = get_cached_resume(category_slug)
    model = get_gemini_model()

    prompt = f"""
{ANALYSIS_SYSTEM_PROMPT}

--- CANDIDATE MASTER PROFILE ---
{resume}

--- TARGET JOB DESCRIPTION ---
{job_description}
"""

    try:
        response = model.generate_content(prompt)
        text_content = response.text.strip()

        # Clean JSON fences if present
        text_content = text_content.removeprefix("```json")
        text_content = text_content.removesuffix("```")
        text_content = text_content.strip()

        return json.loads(text_content)
    except Exception as exc:
        logger.error(f"Gemini evaluation failed: {exc}")
        return {
            "match_score": 0,
            "key_matches": [],
            "missing_skills": [],
            "role_focus": "ERROR",
            "summary_rationale": f"AI analysis failed: {exc!s}"
        }
