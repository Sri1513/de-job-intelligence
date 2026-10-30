# src/engine/agent_bridge.py
"""Agent staging bridge: stage jobs found by ANY MCP client.

Lets any MCP client stage jobs it found by browsing a portal as the user
(LinkedIn, Indeed, Dice, a company career page) into the SAME saved_jobs
table the pipeline writes — so agent-found jobs appear in the MAIN dashboard
feed (/api/jobs filters only on job_category, never on source).

Deliberately separate from run_batch_ingestion, which stays exactly as it
is (JobSpy + Dice scraping). The bridge takes already-read postings: the
calling client did the browsing itself — human pace, logged-in session, so
the portal's own filters actually apply — and passes full posting details.

Flow per call:
  1. Validate every job. title, company, and an http(s) posting URL are
     required: stage_raw_jobs silently drops rows without them, so the
     bridge reports those rows as invalid instead of losing them quietly.
  2. Preserve extra detail fields the client captured (workplace,
     duration, rate text, poster/recruiter, apply method, ...) into the
     row's metadata JSON. Fields stage_raw_jobs already consumes (title,
     company, URL, location, source, description, pay, employment type,
     sponsorship, date posted) stay in their columns. Unknown future
     fields are preserved the same way, so the schema never needs a code
     change for a new detail — that is what makes the bridge dynamic.
  3. Stage via stage_raw_jobs: deterministic job_id (li-<id> for LinkedIn
     URLs, else company+title hash), dedupe against existing rows,
     zero-cost local TF-IDF fit score, status PENDING.
  4. Optionally evaluate ONLY the newly staged ids with the evaluator
     variant of run_backfill_batch (Groq primary, Gemini fallback) — the
     same evaluator ingestion uses. NOTE: the worker variant of
     run_backfill_batch ignores its job_ids argument and evaluates the
     oldest unprocessed queue instead, so it is NOT used here; importing
     the wrong one would evaluate unrelated rows.

Concurrency: staging is one fast transaction and returns synchronously
with job_ids, mirroring retry_job_evaluations. Do not run two staging
calls at once for the same jobs. For batches over ~25 jobs with
evaluation on, prefer run_evaluation=false plus a later
retry_job_evaluations call, to stay inside MCP client stream timeouts.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.core.database import get_db_connection
from src.engine.evaluator import run_backfill_batch
from src.engine.pipeline_utils import generate_job_id, stage_raw_jobs

logger = logging.getLogger("de-job-intelligence.agent_bridge")

MAX_AGENT_JOBS = 100
MAX_EXTRA_STRING = 2000

# Fields stage_raw_jobs already consumes into columns or computed metadata.
# Everything else scalar on the incoming job is preserved into metadata.
_STAGING_KEYS = frozenset(
    {
        "title",
        "company",
        "job_url",
        "location",
        "source",
        "site",
        "description",
        "job_description",
        "min_amount",
        "max_amount",
        "employment_type",
        "sponsorship",
        "date_posted",
        "metadata",
    }
)


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _normalize_job(job: Any, default_source: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate one incoming job; return (normalized job, None) or (None, reason)."""
    if not isinstance(job, dict):
        return None, "job is not an object"
    title = str(job.get("title") or "").strip()
    company = str(job.get("company") or "").strip()
    job_url = str(job.get("job_url") or "").strip()
    if not title:
        return None, "missing title"
    if not company:
        return None, "missing company"
    if not job_url:
        return None, "missing job_url (the posting URL is required for dedupe and the dashboard link)"
    if not (job_url.startswith("http://") or job_url.startswith("https://")):
        return None, "job_url must be an http(s) posting URL"

    portal = str(job.get("source") or job.get("site") or default_source or "Agent").strip()
    normalized = dict(job)
    normalized["title"] = title
    normalized["company"] = company
    normalized["job_url"] = job_url
    normalized["source"] = portal

    extras: Dict[str, Any] = {}
    for key, value in job.items():
        if key in _STAGING_KEYS or value is None:
            continue
        if isinstance(value, str):
            extras[key] = value[:MAX_EXTRA_STRING]
        elif isinstance(value, (int, float, bool)) and _json_safe(value):
            extras[key] = value
    client_meta = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    client_meta = {k: v for k, v in client_meta.items() if _json_safe(v)}
    # Bridge facts win: these rows were found by an agent, on this portal.
    merged_meta = {**client_meta, **extras, "found_via": "agent", "agent_portal": portal}
    normalized["metadata"] = merged_meta
    return normalized, None


def stage_agent_jobs_workflow(
    jobs: List[Dict[str, Any]],
    job_category: str = "data_engineering",
    run_evaluation: bool = True,
    default_source: str = "Agent",
) -> Dict[str, Any]:
    """Stage agent-browsed postings into saved_jobs; optionally AI-evaluate the new rows."""
    if not isinstance(jobs, list):
        return {"status": "error", "message": "jobs must be a list of job objects."}
    if len(jobs) > MAX_AGENT_JOBS:
        return {
            "status": "error",
            "message": (
                f"At most {MAX_AGENT_JOBS} jobs per call; split larger batches "
                "into chunks and call again."
            ),
        }
    if not jobs:
        return {"status": "warning", "message": "No jobs provided; nothing staged.", "staged": []}

    job_category = (job_category or "data_engineering").strip() or "data_engineering"
    default_source = (default_source or "Agent").strip() or "Agent"

    valid_jobs: List[Dict[str, Any]] = []
    valid_ids: List[str] = []
    invalid: List[Dict[str, Any]] = []
    for job in jobs:
        normalized, reason = _normalize_job(job, default_source)
        if normalized is None:
            invalid.append(
                {
                    "title": str(job.get("title")) if isinstance(job, dict) else None,
                    "company": str(job.get("company")) if isinstance(job, dict) else None,
                    "reason": reason,
                }
            )
            continue
        valid_jobs.append(normalized)
        valid_ids.append(
            generate_job_id(normalized["company"], normalized["title"], normalized["job_url"])
        )

    if not valid_jobs:
        return {
            "status": "warning",
            "message": "No valid jobs to stage; see invalid for reasons.",
            "staged": [],
            "already_present": [],
            "invalid": invalid,
        }

    with get_db_connection() as conn:
        stats = stage_raw_jobs(
            conn=conn, jobs=valid_jobs, job_category=job_category, default_source=default_source
        )

    staged_ids = [s["job_id"] for s in stats.get("staged_jobs", [])]
    staged_set = set(staged_ids)
    already_present = [jid for jid in valid_ids if jid not in staged_set]

    evaluated = 0
    evaluation_error: Optional[str] = None
    if run_evaluation and staged_ids:
        try:
            summary = run_backfill_batch(
                limit=len(staged_ids), job_ids=sorted(staged_ids), job_category=job_category
            )
            evaluated = int(summary.get("successful", 0) or 0)
        except Exception as exc:  # staging stands even if evaluation fails
            logger.exception("Agent-bridge evaluation failed for %s", staged_ids)
            evaluation_error = str(exc)

    result: Dict[str, Any] = {
        "status": "success",
        "job_category": job_category,
        "staged": staged_ids,
        "already_present": already_present,
        "invalid": invalid,
        "saved": stats.get("saved", 0),
        "updated": stats.get("updated", 0),
        "skipped": stats.get("skipped", 0),
        "evaluated": evaluated,
        "message": (
            f"Staged {len(staged_ids)} new job(s), {len(already_present)} already present, "
            f"{len(invalid)} invalid, {evaluated} AI-evaluated."
        ),
        "dashboard": (
            f"Staged rows appear in the main dashboard feed under "
            f"job_category '{job_category}' on refresh; verify with search_saved_jobs."
        ),
    }
    if evaluation_error:
        result["evaluation_error"] = evaluation_error
    return result
