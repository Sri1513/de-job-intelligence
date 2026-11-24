# src/protocols/dispatcher.py
"""
MCP tool dispatch layer.

Industry-standard layout: a declarative tool registry (TOOL_HANDLERS) maps each
MCP tool name to a single async handler. Transport code (app.py) never branches
on tool names; business logic lives in engine/workers/synthesis. Adding a tool
means writing one handler and one registry entry.
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from urllib.parse import quote

from src.core.database import get_db_connection, get_job_by_id
from src.core.tailoring_graph import run_tailoring_graph
from src.dashboard.app import (
    LOCKSCREEN_MILESTONE_STEPS,
    LOCKSCREEN_SITE_URL,
    _derive_lockscreen_deadline,
)
from src.engine.agent_bridge import stage_agent_jobs_workflow
from src.engine.alert_pipeline import process_whatsapp_job_alert
from src.engine.ingestion import run_batch_ingestion_workflow
from src.protocols.scopes import require_tool_scope
from src.protocols.validation import validate_tool_arguments
from src.synthesis.gdrive_docs import generate_resume_from_llm_payload
from src.synthesis.prompt_builder import build_job_tailoring_prompt
from src.workers.backfill_worker import run_backfill_batch
from src.workers.email_pipeline import run_email_pipeline

logger = logging.getLogger("de-job-intelligence.dispatcher")


def _search_jobs(
    category: str = "data_engineering", min_score: int = 0, status: str = "ALL", limit: int = 10
) -> list:
    conditions = ["job_category ILIKE %s"]
    params = [f"%{category}%"]

    if status and status.upper() != "ALL":
        conditions.append("ai_status = %s")
        params.append(status.upper())

    query = f"""
        SELECT job_id, title, company, location, is_remote, fit_score, ai_status, saved_at, job_url, apply_status
        FROM saved_jobs
        WHERE {" AND ".join(conditions)}
        ORDER BY saved_at DESC NULLS LAST
        LIMIT %s;
    """
    params.append(limit)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            results = []
            for r in rows:
                row_dict = dict(r)
                if row_dict.get("saved_at"):
                    row_dict["saved_at"] = str(row_dict["saved_at"])
                try:
                    score = int(float(row_dict.get("fit_score") or 0))
                except (ValueError, TypeError):
                    score = 0
                if score >= min_score:
                    row_dict["fit_score"] = score
                    results.append(row_dict)
            return results


def _queue_job_for_apply(job_id: str) -> dict[str, Any]:
    """Sets a job's apply_status to QUEUED in PostgreSQL.

    Refuses to re-queue jobs that were already submitted, mirroring the
    dashboard's 409 guard, so a repeated tool call can never double-apply.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT apply_status FROM saved_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
            if not row:
                return {"success": False, "error": f"Job '{job_id}' not found in saved_jobs."}
            if dict(row).get("apply_status") == "SUBMITTED":
                return {
                    "success": False,
                    "error": f"Job '{job_id}' was already submitted; refusing to re-queue.",
                }
            cur.execute(
                """
                UPDATE saved_jobs
                SET apply_status = 'QUEUED'
                WHERE job_id = %s
                RETURNING job_id, title, company, job_url, apply_status;
                """,
                (job_id,),
            )
            updated = dict(cur.fetchone())
        conn.commit()

    return {
        "success": True,
        "message": f"Job {job_id} successfully queued for browser application.",
        "job": updated,
    }


# ---------------------------------------------------------------------------
# Tool handlers: one async function per MCP tool. Each takes the raw arguments
# dict and returns a JSON-serializable result (or raises for invalid input).
# ---------------------------------------------------------------------------


async def _handle_prepare_job_tailoring_prompt(arguments: Dict[str, Any]) -> Any:
    job_id = arguments.get("job_id")
    if not job_id:
        raise ValueError("Argument 'job_id' is required.")
    return await asyncio.to_thread(build_job_tailoring_prompt, job_id)


# Sprint 4, Lesson 4: the graph as an MCP tool. The old
# prepare_job_tailoring_prompt stays untouched (it returns the bundle);
# this one runs the full graph and returns the verified draft.
# Gated on TAILORING_GRAPH_ENABLED (default off): when off it answers
# with a clean error instead of running.
def _tailoring_graph_enabled() -> bool:
    return os.environ.get("TAILORING_GRAPH_ENABLED", "").lower() in ("1", "true", "yes")


async def _handle_run_tailoring_graph(arguments: Dict[str, Any]) -> Any:
    job_id = arguments.get("job_id")
    if not job_id:
        raise ValueError("Argument 'job_id' is required.")
    if not _tailoring_graph_enabled():
        return {
            "success": False,
            "error": "run_tailoring_graph is disabled (TAILORING_GRAPH_ENABLED is not set).",
        }
    result = await asyncio.to_thread(run_tailoring_graph, job_id)
    return {
        "success": result.get("error") is None,
        "job_id": job_id,
        "draft": result.get("draft"),
        "selected_bullets": result.get("selected_bullets"),
        "verification": result.get("verification"),
        "draft_attempts": result.get("draft_attempts"),
        "error": result.get("error"),
    }


async def _handle_export_tailored_resume(arguments: Dict[str, Any]) -> Any:
    llm_payload = arguments.get("llm_payload") or arguments.get("tailored_content") or arguments
    if isinstance(llm_payload, str):
        try:
            llm_payload = json.loads(llm_payload)
        except Exception:
            pass

    company = arguments.get("company_name") or arguments.get("company")
    title = arguments.get("job_title") or arguments.get("title")
    default_title = (
        f"{company} - Tailored Resume - {title}"
        if (company and title)
        else "Sri Omkar Dumpa - Tailored Resume"
    )
    doc_title = arguments.get("document_title") or default_title

    doc_url = await asyncio.to_thread(
        generate_resume_from_llm_payload,
        llm_payload,
        doc_title,
        arguments.get("slot_headers"),
    )
    return {
        "status": "success",
        "document_url": doc_url,
        "message": f"Resume successfully generated and formatted in Google Docs for {company or 'target role'}.",
    }


async def _handle_get_job_details(arguments: Dict[str, Any]) -> Any:
    job_id = arguments.get("job_id")
    if not job_id:
        raise ValueError("Argument 'job_id' is required.")
    record = await asyncio.to_thread(get_job_by_id, job_id)
    if not record:
        return {"error": f"Job {job_id} not found"}
    if record.get("created_at"):
        record["created_at"] = str(record["created_at"])
    return dict(record)


async def _handle_search_saved_jobs(arguments: Dict[str, Any]) -> Any:
    category = arguments.get("category", "data_engineering")
    min_score = arguments.get("min_score", 0)
    status = arguments.get("status", "ALL")
    limit = arguments.get("limit", 10)
    return await asyncio.to_thread(_search_jobs, category, min_score, status, limit)


async def _handle_queue_job_for_application(arguments: Dict[str, Any]) -> Any:
    job_id = arguments.get("job_id")
    if not job_id:
        raise ValueError("Argument 'job_id' is required.")
    return await asyncio.to_thread(_queue_job_for_apply, job_id)


# ---------------------------------------------------------------------------
# Lockscreen pin (branch friday/lockscreen-pin, owner decision 2026-11-23:
# both ways stay). Gemini pins one saved job as the lock-screen dream
# job; the endpoint returns it first while it still qualifies, then
# falls back to the auto pick. The pin is one job, one row (id = 1).
# ---------------------------------------------------------------------------

PIN_JOB_SQL = """
    SELECT s.job_id, s.title, s.company, s.apply_status, s.posted_at,
           (s.posted_at + 7 >= CURRENT_DATE) AS deadline_live,
           EXISTS (
               SELECT 1 FROM scout.deadline_queue dq
               WHERE dq.job_id = s.job_id
                 AND dq.state = 'done'
                 AND dq.outcome IN ('closed', 'rejected', 'applied')
           ) AS verified_gone
    FROM scout.saved_jobs s
    WHERE s.job_id = %s;
"""

PIN_UPSERT_SQL = """
    INSERT INTO scout.lockscreen_pin (id, job_id, pinned_via)
    VALUES (1, %s, %s)
    ON CONFLICT (id) DO UPDATE
    SET job_id = EXCLUDED.job_id,
        pinned_at = NOW(),
        pinned_via = EXCLUDED.pinned_via;
"""

CLEAR_PIN_SQL = """
    DELETE FROM scout.lockscreen_pin WHERE id = 1 RETURNING job_id;
"""

# Milestone keys/labels, in page order — single source of truth is the
# dashboard's LOCKSCREEN_MILESTONE_STEPS (the handler renders from it).
MILESTONE_LABELS = {step[3]: step[1] for step in LOCKSCREEN_MILESTONE_STEPS}
MILESTONE_KEYS = tuple(MILESTONE_LABELS)

MILESTONE_JOB_SQL = """
    SELECT job_id, title, company FROM scout.saved_jobs WHERE job_id = %s;
"""

MILESTONE_UPSERT_SQL = """
    INSERT INTO scout.job_milestones (job_id, milestone_key, done, done_at, marked_by)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (job_id, milestone_key) DO UPDATE
    SET done = EXCLUDED.done,
        done_at = EXCLUDED.done_at,
        marked_by = EXCLUDED.marked_by;
"""

MILESTONE_LIST_SQL = """
    SELECT milestone_key, done, done_at, marked_by
    FROM scout.job_milestones WHERE job_id = %s;
"""


def _pin_lockscreen_job(job_id: str, pinned_via: str = "gemini") -> dict[str, Any]:
    """Validates one saved job and stores it as the single lockscreen
    pin. Validation failures name the reason; nothing is written on
    failure. The deadline liveness check runs in SQL (DB CURRENT_DATE),
    the same clock the endpoint's pin query uses, and the returned
    deadline is the dashboard's shared derivation (posted_at + 7 days,
    23:59 America/New_York)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(PIN_JOB_SQL, (job_id,))
            row = cur.fetchone()
        if row is None:
            return {
                "success": False,
                "error": f"Job '{job_id}' not found in saved_jobs.",
            }
        job = dict(row)
        if job.get("posted_at") is None:
            return {
                "success": False,
                "error": f"Job '{job_id}' has no posted date, so no 7-day "
                "deadline can be derived; it cannot be pinned.",
            }
        if not job.get("deadline_live"):
            deadline = job["posted_at"] + timedelta(days=7)
            return {
                "success": False,
                "error": f"Job '{job_id}' deadline passed on {deadline}; "
                "it cannot be pinned.",
            }
        if (job.get("apply_status") or "") != "IDLE":
            return {
                "success": False,
                "error": f"Job '{job_id}' is not open "
                f"(apply_status={job.get('apply_status')}); only IDLE "
                "jobs can be pinned.",
            }
        if job.get("verified_gone"):
            return {
                "success": False,
                "error": f"Job '{job_id}' was verified closed, rejected, "
                "or applied; it cannot be pinned.",
            }
        with conn.cursor() as cur:
            cur.execute(PIN_UPSERT_SQL, (job_id, pinned_via))
        conn.commit()
    deadline = _derive_lockscreen_deadline(job["posted_at"])
    return {
        "success": True,
        "job_id": job_id,
        "title": job.get("title"),
        "company": job.get("company"),
        "deadline": deadline.isoformat() if deadline else None,
        "lockscreen_url": (
            f"{LOCKSCREEN_SITE_URL}/lockscreen?job_id={quote(job_id, safe='')}"
        ),
        "message": f"Pinned {job.get('title')} at {job.get('company')} "
        "as the lock-screen job.",
    }


def _clear_lockscreen_pin() -> dict[str, Any]:
    """Deletes the single pin row, if any. Returns which job was
    unpinned (None when nothing was pinned)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(CLEAR_PIN_SQL)
            row = cur.fetchone()
        conn.commit()
    cleared = dict(row) if row else None
    return {
        "success": True,
        "cleared": cleared is not None,
        "cleared_job_id": cleared.get("job_id") if cleared else None,
        "message": (
            f"Removed the lock-screen pin ({cleared['job_id']})."
            if cleared
            else "No lock-screen pin was set."
        ),
    }


def _mark_job_milestone(
    job_id: str, milestone: str, done: bool = True, marked_by: str = "gemini"
) -> dict[str, Any]:
    """Marks one lock-screen milestone done (or re-opens it) for a saved
    job. The lock-screen page reads scout.job_milestones, so this is how
    the owner or Gemini drives the check-offs: the step happened, so it
    gets marked — never the other way round. Unknown job or unknown
    milestone key names the reason; nothing is written on failure."""
    if milestone not in MILESTONE_KEYS:
        return {
            "success": False,
            "error": f"Unknown milestone '{milestone}'. Known milestones: "
            f"{', '.join(MILESTONE_KEYS)}.",
        }
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MILESTONE_JOB_SQL, (job_id,))
            row = cur.fetchone()
        if row is None:
            return {
                "success": False,
                "error": f"Job '{job_id}' not found in saved_jobs.",
            }
        job = dict(row)
        with conn.cursor() as cur:
            cur.execute(
                MILESTONE_UPSERT_SQL,
                (
                    job_id,
                    milestone,
                    done,
                    datetime.now(timezone.utc) if done else None,
                    marked_by,
                ),
            )
        conn.commit()
    milestones = _list_job_milestones(job_id)
    return {
        "success": True,
        "job_id": job_id,
        "title": job.get("title"),
        "company": job.get("company"),
        "milestone": milestone,
        "done": done,
        "marked_by": marked_by,
        "milestones": milestones.get("milestones", []),
        "lockscreen_url": (
            f"{LOCKSCREEN_SITE_URL}/lockscreen?job_id={quote(job_id, safe='')}"
        ),
        "message": f"Marked '{MILESTONE_LABELS[milestone]}' "
        f"{'done' if done else 'not done'} for {job.get('title')} at "
        f"{job.get('company')}.",
    }


def _list_job_milestones(job_id: str) -> dict[str, Any]:
    """Returns all seven lock-screen milestones for a saved job with
    their done-states, in page order, as the page renders them. Missing
    rows read as not done — except 'saved', which is done by definition
    (the job row exists)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MILESTONE_JOB_SQL, (job_id,))
            row = cur.fetchone()
        if row is None:
            return {
                "success": False,
                "error": f"Job '{job_id}' not found in saved_jobs.",
            }
        job = dict(row)
        with conn.cursor() as cur:
            cur.execute(MILESTONE_LIST_SQL, (job_id,))
            rows = cur.fetchall()
        marked = {}
        for r in rows:
            key = r["milestone_key"] if isinstance(r, dict) else r[0]
            done = r["done"] if isinstance(r, dict) else r[1]
            done_at = r["done_at"] if isinstance(r, dict) else r[2]
            by = r["marked_by"] if isinstance(r, dict) else r[3]
            marked[key] = {
                "done": bool(done),
                "done_at": done_at.isoformat() if done_at is not None else None,
                "marked_by": by,
            }
    milestones = [
        {
            "key": key,
            "label": MILESTONE_LABELS[key],
            "done": marked.get(key, {"done": key == "saved"})["done"],
            "done_at": marked.get(key, {}).get("done_at"),
            "marked_by": marked.get(key, {}).get("marked_by"),
        }
        for key in MILESTONE_KEYS
    ]
    return {
        "success": True,
        "job_id": job_id,
        "title": job.get("title"),
        "company": job.get("company"),
        "milestones": milestones,
    }


async def _handle_pin_lockscreen_job(arguments: Dict[str, Any]) -> Any:
    job_id = arguments.get("job_id")
    if not job_id:
        raise ValueError("Argument 'job_id' is required.")
    return await asyncio.to_thread(
        _pin_lockscreen_job, job_id, arguments.get("pinned_via") or "gemini"
    )


async def _handle_clear_lockscreen_pin(arguments: Dict[str, Any]) -> Any:
    return await asyncio.to_thread(_clear_lockscreen_pin)


async def _handle_mark_job_milestone(arguments: Dict[str, Any]) -> Any:
    job_id = arguments.get("job_id")
    if not job_id:
        raise ValueError("Argument 'job_id' is required.")
    milestone = arguments.get("milestone")
    if not milestone:
        raise ValueError("Argument 'milestone' is required.")
    done = arguments.get("done", True)
    if isinstance(done, str):
        done = done.lower() in ("true", "1", "yes")
    return await asyncio.to_thread(
        _mark_job_milestone,
        job_id,
        milestone,
        bool(done),
        arguments.get("marked_by") or "gemini",
    )


async def _handle_list_job_milestones(arguments: Dict[str, Any]) -> Any:
    job_id = arguments.get("job_id")
    if not job_id:
        raise ValueError("Argument 'job_id' is required.")
    return await asyncio.to_thread(_list_job_milestones, job_id)


async def _handle_run_application_worker(arguments: Dict[str, Any]) -> Any:
    # Lazy import so protocol tests and basic MCP health endpoints don't require browser-use
    from src.workers.apply_worker import process_apply_queue

    limit = int(arguments.get("limit", arguments.get("max_jobs", 3)))
    headless = arguments.get("headless", True)
    if isinstance(headless, str):
        headless = headless.lower() in ("true", "1", "yes")

    results = await process_apply_queue(limit=limit, headless=headless)
    return {
        "success": True,
        "jobs_processed": len(results),
        "results": results,
    }


async def _handle_retry_job_evaluations(arguments: Dict[str, Any]) -> Any:
    limit = int(arguments.get("limit", 5))
    job_ids = arguments.get("job_ids")
    category = arguments.get("category", "data_engineering")
    return await asyncio.to_thread(
        run_backfill_batch, limit=limit, job_ids=job_ids, job_category=category
    )


async def _handle_run_batch_ingestion(arguments: Dict[str, Any]) -> Any:
    search_term = arguments.get("search_term", "Data Engineer")
    location = arguments.get("location", "United States")
    results_wanted = int(arguments.get("results_wanted", 15))
    hours_old = int(arguments.get("hours_old", 48))
    job_category = arguments.get("job_category", "data_engineering")
    c2c_only = bool(arguments.get("c2c_only", False))

    return await asyncio.to_thread(
        run_batch_ingestion_workflow,
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        job_category=job_category,
        c2c_only=c2c_only,
    )


async def _handle_process_job_alert_draft(arguments: Dict[str, Any]) -> Any:
    generate_assets = arguments.get("generate_assets", False)
    if isinstance(generate_assets, str):
        generate_assets = generate_assets.lower() in ("true", "1", "yes")
    return process_whatsapp_job_alert(
        whatsapp_text=arguments.get("whatsapp_text"),
        helper_name=arguments.get("helper_name"),
        helper_email=arguments.get("helper_email"),
        helper_company=arguments.get("helper_company"),
        generate_assets=bool(generate_assets),
    )


async def _handle_run_email_pipeline(arguments: Dict[str, Any]) -> Any:
    limit = int(arguments.get("limit", 5))
    job_category = arguments.get("job_category", "data_engineering")

    return await asyncio.to_thread(
        run_email_pipeline,
        limit=limit,
        job_category=job_category,
    )


async def _handle_stage_agent_jobs(arguments: Dict[str, Any]) -> Any:
    jobs = arguments.get("jobs", [])
    if isinstance(jobs, str):
        try:
            jobs = json.loads(jobs)
        except Exception:
            return {
                "status": "error",
                "message": "jobs must be a list of job objects, not a non-JSON string.",
            }

    return await asyncio.to_thread(
        stage_agent_jobs_workflow,
        jobs=jobs,
        job_category=arguments.get("job_category", "data_engineering"),
        run_evaluation=bool(arguments.get("run_evaluation", True)),
        default_source=arguments.get("default_source", "Agent"),
    )


# ---------------------------------------------------------------------------
# Registry: the single source of truth mapping MCP tool names to handlers.
# ---------------------------------------------------------------------------

TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]] = {
    "prepare_job_tailoring_prompt": _handle_prepare_job_tailoring_prompt,
    "fetch_job_for_tailoring": _handle_prepare_job_tailoring_prompt,
    "run_tailoring_graph": _handle_run_tailoring_graph,
    "export_tailored_resume": _handle_export_tailored_resume,
    "generate_tailored_resume": _handle_export_tailored_resume,
    "get_job_details": _handle_get_job_details,
    "search_saved_jobs": _handle_search_saved_jobs,
    "queue_job_for_application": _handle_queue_job_for_application,
    "pin_lockscreen_job": _handle_pin_lockscreen_job,
    "clear_lockscreen_pin": _handle_clear_lockscreen_pin,
    "mark_job_milestone": _handle_mark_job_milestone,
    "list_job_milestones": _handle_list_job_milestones,
    "run_application_worker": _handle_run_application_worker,
    "retry_job_evaluations": _handle_retry_job_evaluations,
    "run_batch_ingestion": _handle_run_batch_ingestion,
    "stage_agent_jobs": _handle_stage_agent_jobs,
    "process_job_alert_draft": _handle_process_job_alert_draft,
    "run_email_pipeline": _handle_run_email_pipeline,
}


# ---------------------------------------------------------------------------
# Background pipeline runs: long-running ingestion/email tools.
# ---------------------------------------------------------------------------
# Gmail + JobSpy + LLM evaluation takes minutes, but some MCP client proxies
# (notably Gemini's) drop the HTTP stream after ~30-60s. The run still
# completes server-side and stages jobs, so the client sees a timeout for a
# run that actually succeeded. These tools therefore return immediately with
# a "started" result while the real work continues in a background task; the
# client checks staged jobs via search_saved_jobs.
#
# One pipeline run at a time, shared across both tools: concurrent
# ingestion/email runs double the LinkedIn request rate (429s) and duplicate
# evaluation work. A second call while one is running gets "already_running"
# instead of starting a duplicate.
ASYNC_PIPELINE_TOOLS = frozenset({"run_email_pipeline", "run_batch_ingestion"})

_pipeline_task: asyncio.Task | None = None
_pipeline_tool: str | None = None


async def _run_pipeline_in_background(tool_name, handler, arguments) -> None:
    try:
        result = await handler(arguments)
        logger.info("Background pipeline run finished: %s -> %s", tool_name, result)
    except Exception:
        logger.exception("Background pipeline run failed: %s", tool_name)


def _clear_pipeline_slot(task: asyncio.Task) -> None:
    """Clear the run slot once a background run finishes (only if current)."""
    global _pipeline_task, _pipeline_tool
    if _pipeline_task is task:
        _pipeline_task = None
        _pipeline_tool = None


def _start_pipeline_run(tool_name, handler, arguments) -> Dict[str, Any]:
    """Start a pipeline run in the background, or refuse a duplicate.

    Synchronous (no await between the slot check and assignment), so the
    check-and-claim is atomic on the event loop.
    """
    global _pipeline_task, _pipeline_tool
    if _pipeline_task is not None and not _pipeline_task.done():
        return {
            "status": "already_running",
            "tool": _pipeline_tool,
            "message": (
                f"A pipeline run ({_pipeline_tool}) is already in progress; "
                "this call did not start a duplicate. "
                "Check staged jobs via search_saved_jobs."
            ),
        }
    _pipeline_tool = tool_name
    _pipeline_task = asyncio.create_task(_run_pipeline_in_background(tool_name, handler, arguments))
    _pipeline_task.add_done_callback(_clear_pipeline_slot)
    logger.info("Background pipeline run started: %s", tool_name)
    return {
        "status": "started",
        "tool": tool_name,
        "message": (
            f"{tool_name} started in the background. Jobs stage as they are "
            "processed; check them via search_saved_jobs."
        ),
    }


async def dispatch_tool_call(tool_name: str, arguments: Dict[str, Any], scopes=None) -> Any:
    """Dispatches a JSON-RPC tool request through the handler registry.

    Scope is checked first (Sprint 2, Batch 2): a caller that lacks the
    tool's scope gets InsufficientScopeError before validation or any
    handler runs, so it learns nothing about the tool's arguments.
    `scopes=None` means "no scope information" (direct/internal caller)
    and is unrestricted; an empty set is authenticated-but-unscoped and
    is denied for every tool. Arguments are then Pydantic-validated
    (Sprint 2, Batch 1). Handlers still receive a plain dict (validated,
    defaults applied), so they are untouched.
    """
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: '{tool_name}'"}
    require_tool_scope(tool_name, scopes)
    arguments = validate_tool_arguments(tool_name, arguments)
    if tool_name in ASYNC_PIPELINE_TOOLS:
        return _start_pipeline_run(tool_name, handler, arguments)
    return await handler(arguments)
