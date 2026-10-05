# src/engine/analyzer.py
import json
import logging
import warnings
from typing import Any

from src.core.utils import get_cached_resume
from src.engine.llm_text import generate_text

warnings.filterwarnings("ignore", category=FutureWarning)

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


def evaluate_job_fit(
    job_description: str, category_slug: str = "data_engineering"
) -> dict[str, Any]:
    """
    Evaluates job requirements using Groq API as primary, with Gemini fallback.
    """
    if not job_description or len(job_description.strip()) < 50:
        return {
            "match_score": 0,
            "key_matches": [],
            "missing_skills": [],
            "role_focus": "UNKNOWN",
            "summary_rationale": "Job description insufficient for analysis.",
        }

    resume = get_cached_resume(category_slug)
    user_content = f"""
--- CANDIDATE MASTER PROFILE ---
{resume}

--- TARGET JOB DESCRIPTION ---
{job_description}
"""

    # Config-driven failover chain (config/llm.yaml): tries providers in order,
    # sidelining any that hit quota/errors, with WhatsApp alerts on failover.
    try:
        raw_response = generate_text(
            f"{ANALYSIS_SYSTEM_PROMPT}\n\n{user_content}",
            json_mode=True,
            temperature=0.1,
            task="job-analysis",
        )
    except Exception as exc:
        logger.error(f"❌ All LLM providers failed in analyzer: {exc}")
        return {
            "match_score": 0,
            "key_matches": [],
            "missing_skills": [],
            "role_focus": "ERROR",
            "summary_rationale": "All AI providers failed. See logs for details.",
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
            "summary_rationale": f"JSON parsing failed: {parse_exc}",
        }
