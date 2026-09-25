# src/engine/analyzer.py
import json
import logging
import os
from typing import Any
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

from openai import OpenAI
import google.generativeai as genai

from src.core.config import settings
from src.core.utils import get_cached_resume

logger = logging.getLogger("de-job-intelligence.analyzer")

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

def _clean_and_parse_json(text_content: str) -> dict[str, Any]:
    """Cleans markdown code fences and parses JSON response."""
    text_content = text_content.strip()
    try:
        return json.loads(text_content)
    except json.JSONDecodeError:
        pass
    
    cleaned = text_content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)

def evaluate_job_fit(job_description: str, category_slug: str = "data_engineering") -> dict[str, Any]:
    """
    Evaluates job requirements using Groq API as primary, with Gemini fallback.
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
    user_content = f"""
--- CANDIDATE MASTER PROFILE ---
{resume}

--- TARGET JOB DESCRIPTION ---
{job_description}
"""

    groq_api_key = settings.GROQ_API_KEY or os.getenv("GROQ_API_KEY")
    raw_response = None

    # 1. Try Primary: Groq API
    if groq_api_key:
        try:
            client = OpenAI(
                base_url="[https://api.groq.com/openai/v1](https://api.groq.com/openai/v1)",
                api_key=groq_api_key
            )
            response = client.chat.completions.create(
                model=settings.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.1,
                timeout=30.0
            )
            raw_response = response.choices[0].message.content.strip()
        except Exception as groq_exc:
            logger.warning(f"⚠️ Groq primary evaluation failed in analyzer: {groq_exc}. Falling back to Gemini...")

    # 2. Fallback: Gemini API
    if not raw_response:
        gemini_key = settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
        if gemini_key:
            try:
                genai.configure(api_key=gemini_key)
                model_name = settings.GEMINI_MODEL or "gemini-1.5-flash"
                model = genai.GenerativeModel(model_name)
                gemini_prompt = f"{ANALYSIS_SYSTEM_PROMPT}\n\n{user_content}"
                gemini_resp = model.generate_content(gemini_prompt)
                raw_response = gemini_resp.text.strip()
            except Exception as gemini_exc:
                logger.error(f"❌ Gemini fallback also failed in analyzer: {gemini_exc}")
                return {
                    "match_score": 0,
                    "key_matches": [],
                    "missing_skills": [],
                    "role_focus": "ERROR",
                    "summary_rationale": f"All AI providers failed. Groq & Gemini errors encountered."
                }
        else:
            return {
                "match_score": 0,
                "key_matches": [],
                "missing_skills": [],
                "role_focus": "ERROR",
                "summary_rationale": "Groq failed and no Gemini fallback API key is configured."
            }

    try:
        return _clean_and_parse_json(raw_response)
    except Exception as parse_exc:
        logger.error(f"❌ Failed to parse JSON from analyzer response: {parse_exc}")
        return {
            "match_score": 0,
            "key_matches": [],
            "missing_skills": [],
            "role_focus": "ERROR",
            "summary_rationale": f"JSON parsing failed: {parse_exc}"
        }