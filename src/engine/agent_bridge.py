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
  4. Refresh already-present rows. stage_raw_jobs only heals a placeholder
     description and merges new locations on an existing row: it never
     moves the row's job_category, never merges new detail fields, and
     never touches the sponsorship column. So a row first recorded by
     another path (for example the engine-progress tracker, which files
     rows under job_category 'friday_engine') would otherwise sit
     outside the caller's main feed forever, no matter how complete the
     re-staged posting is. The bridge therefore refreshes each
     already-present row in the same transaction: job_category to the
     caller's category, caller detail fields merged into metadata
     (pipeline-computed keys protected, see below), and the sponsorship
     column when the caller supplied one.
  5. Optionally evaluate the newly staged AND refreshed ids with the
     evaluator variant of run_backfill_batch (Groq primary, Gemini
     fallback) — the same evaluator ingestion uses. NOTE: the worker
     variant of run_backfill_batch ignores its job_ids argument and
     evaluates the oldest unprocessed queue instead, so it is NOT used
     here; importing the wrong one would evaluate unrelated rows.

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


# Metadata keys the pipeline itself computes. A refresh must never let
# caller data overwrite them: locations, source mapping, and fit scores
# stay exactly what the pipeline computed, while caller detail fields
# (workplace, rate text, poster, ...) merge around them.
_REFRESH_PROTECTED_METADATA = frozenset(
    {
        "locations",
        "source",
        "matched_skills",
        "missing_skills",
        "semantic_match",
        "skill_match",
        "retrieved_at",
        "tailoring_signals",
        "ai_extracted_skills",
    }
)


def _refresh_existing_rows(conn, jobs_by_id, job_category):
    """Refresh already-present rows in place; return refreshed job_ids.

    Only touches rows the caller re-staged in this call. A row counts as
    refreshed when at least one of these actually changes: job_category
    to the caller's category, metadata merged with the caller's detail
    fields (protected keys above excluded), or the sponsorship column
    when the caller supplied one. Unchanged rows are left alone, so a
    repeat call with the same data is a no-op for them.
    """
    refreshed = []
    with conn.cursor() as cur:
        for job_id, job in jobs_by_id.items():
            cur.execute(
                "SELECT job_category, metadata, sponsorship FROM saved_jobs WHERE job_id = %s;",
                (job_id,),
            )
            row = cur.fetchone()
            if row is None:
                continue
            if isinstance(row, dict):
                cur_category = row.get("job_category")
                cur_meta = row.get("metadata") or {}
                cur_sponsorship = row.get("sponsorship")
            else:
                cur_category, cur_meta, cur_sponsorship = row[0], row[1] or {}, row[2]
            if isinstance(cur_meta, str):
                try:
                    cur_meta = json.loads(cur_meta)
                except ValueError:
                    cur_meta = {}
            if not isinstance(cur_meta, dict):
                cur_meta = {}
            incoming = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
            merged = {
                **cur_meta,
                **{k: v for k, v in incoming.items() if k not in _REFRESH_PROTECTED_METADATA},
            }
            new_sponsorship = str(job.get("sponsorship") or "").strip()
            updates = []
            params = []
            if cur_category != job_category:
                updates.append("job_category = %s")
                params.append(job_category)
            if merged != cur_meta:
                updates.append("metadata = %s")
                params.append(json.dumps(merged))
            if new_sponsorship and new_sponsorship != (cur_sponsorship or ""):
                updates.append("sponsorship = %s")
                params.append(new_sponsorship)
            if updates:
                params.append(job_id)
                cur.execute(
                    f"UPDATE saved_jobs SET {', '.join(updates)} WHERE job_id = %s;",
                    tuple(params),
                )
                refreshed.append(job_id)
    conn.commit()
    return refreshed


def stage_agent_jobs_workflow(
    jobs: List[Dict[str, Any]],
    job_category: str = "data_engineering",
    run_evaluation: bool = True,
    default_source: str = "Agent",
) -> Dict[str, Any]:
    """Stage agent-browsed postings into saved_jobs; refresh already-present rows; optionally AI-evaluate the new and refreshed rows."""
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
        jobs_by_id = dict(zip(valid_ids, valid_jobs))
        already_present_ids = [
            jid for jid in valid_ids if jid not in {s["job_id"] for s in stats.get("staged_jobs", [])}
        ]
        refreshed_ids = _refresh_existing_rows(conn, {jid: jobs_by_id[jid] for jid in already_present_ids}, job_category)

    staged_ids = [s["job_id"] for s in stats.get("staged_jobs", [])]
    staged_set = set(staged_ids)
    already_present = [jid for jid in valid_ids if jid not in staged_set]

    evaluated = 0
    evaluation_error: Optional[str] = None
    evaluate_ids = sorted(set(staged_ids) | set(refreshed_ids))
    if run_evaluation and evaluate_ids:
        try:
            summary = run_backfill_batch(
                limit=len(evaluate_ids), job_ids=evaluate_ids, job_category=job_category
            )
            evaluated = int(summary.get("successful", 0) or 0)
        except Exception as exc:  # staging stands even if evaluation fails
            logger.exception("Agent-bridge evaluation failed for %s", evaluate_ids)
            evaluation_error = str(exc)

    result: Dict[str, Any] = {
        "status": "success",
        "job_category": job_category,
        "staged": staged_ids,
        "already_present": already_present,
        "refreshed": refreshed_ids,
        "invalid": invalid,
        "saved": stats.get("saved", 0),
        "updated": stats.get("updated", 0),
        "skipped": stats.get("skipped", 0),
        "evaluated": evaluated,
        "message": (
            f"Staged {len(staged_ids)} new job(s), {len(already_present)} already present "
            f"({len(refreshed_ids)} refreshed), "
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
