# src/engine/evaluator.py
import json
import logging
import os
import time
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import google.generativeai as genai
from openai import OpenAI

from src.core.config import settings
from src.core.database import get_db_connection
from src.engine.llm_router import classify_llm_error
from src.engine.matcher import calculate_local_fit_score, get_cached_resume

warnings.filterwarnings("ignore", category=FutureWarning)

logger = logging.getLogger(__name__)


# --- Environment & Key Discovery ---
def _load_env_keys():
    from pathlib import Path

    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k not in os.environ:
                    os.environ[k] = v


_load_env_keys()


class GeminiKeyRotator:
    """Manages a pool of Gemini API keys, automatically rotating on 429 quota exhaustion.

    Two pools exist: the FREE pool (``GEMINI_API_KEY``, ``GEMINI_API_KEY_2``…
    or comma-separated ``GEMINI_API_KEYS``) used by the analysis pipeline, and
    the PAID pool (``GEMINI_PAID_API_KEY``, ``GEMINI_PAID_API_KEY_2``… or
    ``GEMINI_PAID_API_KEYS``) used only as the last resort of the apply-agent
    failover chain. The analysis pipeline never touches paid keys.
    """

    def __init__(self, paid: bool = False):
        _load_env_keys()
        self.paid = paid
        prefix = "GEMINI_PAID_API_KEY" if paid else "GEMINI_API_KEY"
        list_var = "GEMINI_PAID_API_KEYS" if paid else "GEMINI_API_KEYS"
        discovered_keys = []

        raw_keys = os.getenv(list_var, "")
        if raw_keys:
            discovered_keys.extend([k.strip() for k in raw_keys.split(",") if k.strip()])

        for i in range(1, 10):
            var_name = prefix if i == 1 else f"{prefix}_{i}"
            k = os.getenv(var_name)
            if k and k.strip() and k.strip() not in discovered_keys:
                discovered_keys.append(k.strip())

        if not paid and not discovered_keys and os.getenv("GOOGLE_API_KEY"):
            discovered_keys.append(os.getenv("GOOGLE_API_KEY").strip())

        self.keys = discovered_keys
        self.current_idx = 0
        pool = "paid" if paid else "free"
        if not self.keys:
            logger.warning(f"No Gemini {pool} API keys found in environment variables.")
        else:
            logger.info(f"Loaded {len(self.keys)} Gemini {pool} API key(s) into rotation pool.")
            self._configure_active_key()

    def _configure_active_key(self):
        active_key = self.keys[self.current_idx]
        genai.configure(api_key=active_key)
        logger.info(f"Configured Gemini API Key index {self.current_idx + 1} of {len(self.keys)}")

    def rotate_key(self) -> bool:
        if len(self.keys) <= 1:
            logger.warning("Only 1 API key configured; cannot rotate.")
            return False

        self.current_idx = (self.current_idx + 1) % len(self.keys)
        self._configure_active_key()
        return True

    def evaluate_with_gemini(self, prompt: str) -> str:
        if not self.keys:
            pool = "paid" if self.paid else "free"
            raise RuntimeError(f"No Gemini {pool} API keys available for fallback.")
        self._configure_active_key()
        model = genai.GenerativeModel(settings.GEMINI_MODEL)
        response = model.generate_content(prompt)
        return response.text.strip()


# Free pool: used by the analysis pipeline (fit score / sponsorship / skills).
# Paid pool: reserved for the apply-agent failover chain's last resort.
free_rotator = GeminiKeyRotator(paid=False)
paid_rotator = GeminiKeyRotator(paid=True)

# In-process short-circuit: once Groq's free quota is hit during a batch run,
# skip it for the rest of the day instead of failing every job loudly.
_groq_exhausted_until: Optional[datetime] = None


def _groq_exhausted() -> bool:
    return _groq_exhausted_until is not None and datetime.now(timezone.utc) < _groq_exhausted_until


def _mark_groq_exhausted(reason: str) -> None:
    global _groq_exhausted_until
    _groq_exhausted_until = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    logger.warning(
        "evaluator: Groq free quota exhausted (%s); skipping Groq for remaining "
        "jobs until %s",
        reason,
        _groq_exhausted_until.isoformat(),
    )

EVAL_PROMPT = """You are an executive technical recruiter evaluating Data Engineering and DevOps positions.
Analyze the target job against the candidate profile.

CANDIDATE BASE:
{resume_text}

TARGET JOB LISTING:
Job ID: {job_id}
Title: {title}
Company: {company}
Description:
{description}

STRICT INSTRUCTIONS:
1. 'tech_stack': ONLY specific technologies, platforms, tools, and languages (e.g., Python, PySpark, AWS, Snowflake, Airflow, T-SQL, Tableau, Terraform). Do NOT include generic phrases or tasks.
2. 'tailoring_signals': Workflows, domain context, or operational focuses useful for tailoring bullets (e.g., Data Reconciliation, High Uptime, Healthcare Analytics, DOD Clearance).
3. 'note': Max 2 concise sentences. State the immediate candidate fit and the single most critical gap/blocker.

Return a STRICT JSON object (no markdown, no backticks):
{{
  "job_id": "{job_id}",
  "employment_type": "Full-time | Contract | Unknown",
  "sponsorship": "Available | Not Mentioned | No Sponsorship",
  "tech_stack": ["Tool1", "Tool2"],
  "tailoring_signals": ["Focus1", "Focus2"],
  "note": "Strong match on core AWS/Spark stack. Main gap is legacy T-SQL forensics requirement."
}}
"""


def evaluate_job_with_fallback(job: Dict[str, Any], resume_text: str) -> Dict[str, Any]:
    """Evaluates a job via Groq (free) first, then the Gemini FREE key pool.

    The paid Gemini pool is never used here — analysis stays on free tiers by
    policy. Every provider decision is logged with the job id.
    """
    prompt = EVAL_PROMPT.format(
        resume_text=resume_text[:4000],
        job_id=job["job_id"],
        title=job.get("title", "Unknown"),
        company=job.get("company", "Unknown"),
        description=job.get("description", "")[:4000],
    )

    groq_api_key = os.getenv("GROQ_API_KEY")
    raw_response = None
    serving_provider = "none"

    # 1. Try Primary: Groq API (free tier)
    if groq_api_key and not _groq_exhausted():
        try:
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_api_key)
            response = client.chat.completions.create(
                model=settings.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": EVAL_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                timeout=30.0,
            )
            raw_response = response.choices[0].message.content.strip()
            serving_provider = f"groq({settings.GROQ_MODEL})"
        except Exception as e:
            kind = classify_llm_error(e)
            logger.warning(
                "evaluator: Groq evaluation failed for %s [%s]: %s. Failing over to Gemini free pool...",
                job["job_id"],
                kind,
                str(e)[:200],
            )
            if kind in ("quota", "billing"):
                _mark_groq_exhausted(str(e)[:120])
    elif groq_api_key and _groq_exhausted():
        logger.debug(
            "evaluator: skipping Groq for %s (quota exhausted earlier this run)",
            job["job_id"],
        )

    # 2. Fallback: Gemini FREE key pool (never paid)
    if not raw_response:
        attempts = 0
        max_gemini_retries = len(free_rotator.keys) + 1 if free_rotator.keys else 1
        while attempts < max_gemini_retries:
            try:
                raw_response = free_rotator.evaluate_with_gemini(prompt)
                serving_provider = f"gemini-free(key {free_rotator.current_idx + 1}/{len(free_rotator.keys)})"
                break
            except Exception as ge:
                kind = classify_llm_error(ge)
                if kind in ("quota", "billing"):
                    logger.warning(
                        "evaluator: quota exhausted on Gemini free key %d/%d [%s]. Rotating...",
                        free_rotator.current_idx + 1,
                        len(free_rotator.keys),
                        kind,
                    )
                    rotated = free_rotator.rotate_key()
                    if not rotated:
                        raise RuntimeError(
                            "All Gemini free API keys in pool exhausted their quota."
                        ) from ge
                else:
                    logger.warning(
                        "evaluator: Gemini free fallback attempt failed [%s]: %s",
                        kind,
                        str(ge)[:200],
                    )
                    free_rotator.rotate_key()
                attempts += 1
                time.sleep(2)

        if not raw_response:
            raise RuntimeError(f"All evaluation providers failed for job {job['job_id']}.")

    logger.info(
        "evaluator: job %s evaluated via %s", job["job_id"], serving_provider
    )

    # Clean response formatting
    if raw_response.startswith("```"):
        raw_response = raw_response.strip("`").replace("json\n", "", 1).strip()
    return json.loads(raw_response)


def run_backfill_batch(
    limit: int = 5,
    job_ids: Optional[List[str]] = None,
    job_category: str = "data_engineering",
    *args,
    **kwargs,
) -> Dict[str, Any]:
    """Processes pending or failed jobs using Groq primary with Gemini fallback."""
    resume_text = get_cached_resume(job_category)
    jobs_to_process = []

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if job_ids and len(job_ids) > 0:
                cur.execute(
                    """
                    SELECT job_id, title, company, description, metadata
                    FROM saved_jobs
                    WHERE job_id = ANY(%s);
                    """,
                    (job_ids,),
                )
            else:
                cur.execute(
                    """
                    SELECT job_id, title, company, description, metadata
                    FROM saved_jobs
                    WHERE (
                        ai_status IN ('PENDING', 'FAILED')
                        OR LEFT(notes, 18) = 'AI analysis failed'
                        OR metadata->'ai_extracted_skills' IS NULL
                        OR metadata->>'ai_extracted_skills' IN ('[]', 'null', '')
                    )
                    ORDER BY saved_at DESC NULLS LAST
                    LIMIT %s;
                    """,
                    (limit,),
                )
            for r in cur.fetchall():
                jobs_to_process.append(
                    {
                        "job_id": r["job_id"] if isinstance(r, dict) else r[0],
                        "title": r["title"] if isinstance(r, dict) else r[1],
                        "company": r["company"] if isinstance(r, dict) else r[2],
                        "description": r["description"] if isinstance(r, dict) else r[3],
                        "metadata": (r["metadata"] if isinstance(r, dict) else r[4]) or {},
                    }
                )

    if not jobs_to_process:
        return {"status": "success", "message": "No pending jobs found.", "processed": 0}

    successful = 0
    failed = 0
    results = []

    for idx, job in enumerate(jobs_to_process):
        job_id = job["job_id"]
        try:
            ai_data = evaluate_job_with_fallback(job, resume_text)
            tech_stack = ai_data.get("tech_stack") or ai_data.get("extracted_skills", [])
            tailoring_signals = ai_data.get("tailoring_signals", [])

            score_data = calculate_local_fit_score(
                resume_text=resume_text,
                job_description=job.get("description", ""),
                job_title=job.get("title", ""),
                job_category=job_category,
                ai_extracted_skills=tech_stack,
            )

            metadata = job["metadata"]
            metadata.update(
                {
                    "matched_skills": score_data.get("matched_skills", []),
                    "missing_skills": score_data.get("missing_skills", []),
                    "semantic_match": score_data.get("semantic_match", 0),
                    "skill_match": score_data.get("skill_match", 0),
                    "ai_extracted_skills": tech_stack,
                    "tailoring_signals": tailoring_signals,
                }
            )

            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE saved_jobs SET
                            fit_score = %s,
                            notes = %s,
                            employment_type = %s,
                            sponsorship = %s,
                            metadata = %s,
                            ai_status = 'PROCESSED',
                            ai_error = NULL
                        WHERE job_id = %s;
                        """,
                        (
                            str(score_data["score"]),
                            ai_data.get("note", "Auto-evaluated job."),
                            ai_data.get("employment_type", "Unknown"),
                            ai_data.get("sponsorship", "Not Mentioned"),
                            json.dumps(metadata),
                            job_id,
                        ),
                    )
                    conn.commit()

            successful += 1
            results.append({"job_id": job_id, "score": score_data["score"], "status": "PROCESSED"})
            logger.info(f"Evaluated {job_id}: score {score_data['score']}")

            if idx + 1 < len(jobs_to_process):
                time.sleep(1.5)

        except Exception as e:
            failed += 1
            logger.error(f"Failed {job_id}: {e}")
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE saved_jobs SET ai_status = 'FAILED', ai_error = %s WHERE job_id = %s;",
                        (str(e), job_id),
                    )
                    conn.commit()
            results.append({"job_id": job_id, "status": "FAILED", "error": str(e)})

    return {
        "status": "completed",
        "attempted": len(jobs_to_process),
        "successful": successful,
        "failed": failed,
        "results": results,
    }
