# src/protocols/dispatcher.py
import asyncio
import logging
from typing import Any, Dict
from src.core.database import get_db_connection, get_job_by_id
from src.synthesis.prompt_builder import build_job_tailoring_prompt
from src.synthesis.gdrive_docs import generate_resume_from_llm_payload
from src.workers.backfill_worker import run_backfill_batch
from src.engine.ingestion import run_batch_ingestion_workflow
from src.synthesis.prompt_builder import build_tailoring_prompt
from src.engine.alert_pipeline import process_whatsapp_job_alert
from src.workers.email_pipeline import run_email_pipeline

logger = logging.getLogger("de-job-intelligence.dispatcher")


def _search_jobs(category: str = "data_engineering", min_score: int = 0, status: str = "ALL", limit: int = 10) -> list:
    conditions = ["job_category ILIKE %s"]
    params = [f"%{category}%"]

    if status and status.upper() != "ALL":
        conditions.append("ai_status = %s")
        params.append(status.upper())

    query = f"""
        SELECT job_id, title, company, location, is_remote, fit_score, ai_status, saved_at, job_url
        FROM saved_jobs
        WHERE {' AND '.join(conditions)}
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
                # Filter score if requested
                try:
                    score = int(float(row_dict.get("fit_score") or 0))
                except (ValueError, TypeError):
                    score = 0
                if score >= min_score:
                    row_dict["fit_score"] = score
                    results.append(row_dict)
            return results


async def dispatch_tool_call(tool_name: str, arguments: Dict[str, Any]) -> Any:
    """Dispatches JSON-RPC tool requests asynchronously."""
    if tool_name == "prepare_job_tailoring_prompt":
        job_id = arguments.get("job_id")
        if not job_id:
            raise ValueError("Argument 'job_id' is required.")
        return await asyncio.to_thread(build_job_tailoring_prompt, job_id)

    elif tool_name in ("export_tailored_resume", "generate_tailored_resume"):
        llm_payload = arguments.get("llm_payload") or arguments.get("tailored_content") or arguments
        if isinstance(llm_payload, str):
            import json
            try:
                llm_payload = json.loads(llm_payload)
            except Exception:
                pass

        company = arguments.get("company_name") or arguments.get("company")
        title = arguments.get("job_title") or arguments.get("title")
        default_title = f"{company} - Tailored Resume - {title}" if (company and title) else "Sri Omkar Dumpa - Tailored Resume"
        doc_title = arguments.get("document_title") or default_title

        doc_url = await asyncio.to_thread(generate_resume_from_llm_payload, llm_payload, doc_title)
        return {
            "status": "success",
            "document_url": doc_url,
            "message": f"Resume successfully generated and formatted in Google Docs for {company or 'target role'}.",
        }

    elif tool_name == "get_job_details":
        job_id = arguments.get("job_id")
        if not job_id:
            raise ValueError("Argument 'job_id' is required.")
        record = await asyncio.to_thread(get_job_by_id, job_id)
        if not record:
            return {"error": f"Job {job_id} not found"}
        if record.get("created_at"):
            record["created_at"] = str(record["created_at"])
        return dict(record)

    elif tool_name == "search_saved_jobs":
        category = arguments.get("category", "data_engineering")
        min_score = arguments.get("min_score", 0)
        status = arguments.get("status", "ALL")
        limit = arguments.get("limit", 10)
        return await asyncio.to_thread(_search_jobs, category, min_score, status, limit)

    elif tool_name == "retry_job_evaluations":
        limit = int(arguments.get("limit", 5))
        job_ids = arguments.get("job_ids")
        category = arguments.get("category", "data_engineering")
        return await asyncio.to_thread(run_backfill_batch, limit=limit, job_ids=job_ids, job_category=category)

    elif tool_name == "run_batch_ingestion":
        search_term = arguments.get("search_term", "Data Engineer")
        location = arguments.get("location", "Remote")
        results_wanted = int(arguments.get("results_wanted", 5))
        hours_old = int(arguments.get("hours_old", 48))
        job_category = arguments.get("job_category", "data_engineering")

        return await asyncio.to_thread(
            run_batch_ingestion_workflow,
            search_term=search_term,
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            job_category=job_category
        )
    
    elif tool_name in ("fetch_job_for_tailoring", "prepare_job_tailoring_prompt"):
        job_id = arguments.get("job_id")
        return await asyncio.to_thread(build_tailoring_prompt, job_id=job_id)

    elif tool_name == "export_tailored_resume":
        llm_payload = arguments.get("llm_payload")
        if not llm_payload:
            raise ValueError("Argument 'llm_payload' is required.")
    
    elif tool_name == "process_job_alert_draft":
        return process_whatsapp_job_alert(
            whatsapp_text=arguments.get("whatsapp_text"),
            helper_name=arguments.get("helper_name"),
            helper_email=arguments.get("helper_email"),
            helper_company=arguments.get("helper_company")
        )

    elif tool_name == "run_email_pipeline":
        limit = int(arguments.get("limit", 5))
        job_category = arguments.get("job_category", "data_engineering")

        return await asyncio.to_thread(
            run_email_pipeline,
            limit=limit,
            job_category=job_category
        )
    
    else:
            return {"error": f"Unknown tool: '{tool_name}'"}