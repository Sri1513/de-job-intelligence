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
from collections.abc import Awaitable, Callable
from typing import Any, Dict

from src.core.database import get_db_connection, get_job_by_id
from src.engine.alert_pipeline import process_whatsapp_job_alert
from src.engine.ingestion import run_batch_ingestion_workflow
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
    results_wanted = int(arguments.get("results_wanted", 5))
    hours_old = int(arguments.get("hours_old", 48))
    job_category = arguments.get("job_category", "data_engineering")

    return await asyncio.to_thread(
        run_batch_ingestion_workflow,
        search_term=search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        job_category=job_category,
    )


async def _handle_process_job_alert_draft(arguments: Dict[str, Any]) -> Any:
    return process_whatsapp_job_alert(
        whatsapp_text=arguments.get("whatsapp_text"),
        helper_name=arguments.get("helper_name"),
        helper_email=arguments.get("helper_email"),
        helper_company=arguments.get("helper_company"),
    )


async def _handle_run_email_pipeline(arguments: Dict[str, Any]) -> Any:
    limit = int(arguments.get("limit", 5))
    job_category = arguments.get("job_category", "data_engineering")

    return await asyncio.to_thread(
        run_email_pipeline,
        limit=limit,
        job_category=job_category,
    )


# ---------------------------------------------------------------------------
# Registry: the single source of truth mapping MCP tool names to handlers.
# ---------------------------------------------------------------------------

TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Awaitable[Any]]] = {
    "prepare_job_tailoring_prompt": _handle_prepare_job_tailoring_prompt,
    "fetch_job_for_tailoring": _handle_prepare_job_tailoring_prompt,
    "export_tailored_resume": _handle_export_tailored_resume,
    "generate_tailored_resume": _handle_export_tailored_resume,
    "get_job_details": _handle_get_job_details,
    "search_saved_jobs": _handle_search_saved_jobs,
    "queue_job_for_application": _handle_queue_job_for_application,
    "run_application_worker": _handle_run_application_worker,
    "retry_job_evaluations": _handle_retry_job_evaluations,
    "run_batch_ingestion": _handle_run_batch_ingestion,
    "process_job_alert_draft": _handle_process_job_alert_draft,
    "run_email_pipeline": _handle_run_email_pipeline,
}


async def dispatch_tool_call(tool_name: str, arguments: Dict[str, Any]) -> Any:
    """Dispatches a JSON-RPC tool request through the handler registry."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: '{tool_name}'"}
    return await handler(arguments)
