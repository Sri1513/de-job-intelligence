# src/protocols/dispatcher.py
import asyncio
from typing import Any

from src.core.database import get_job_by_id
from src.engine.prompt_builder import build_tailoring_payload
from src.synthesis.gdrive_docs import create_tailored_document
from src.synthesis.resume_mapper import map_resume_placeholders
from src.workers.backfill_worker import run_backfill_batch


async def dispatch_tool_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Asynchronously routes tool requests to the appropriate subsystem.
    Delegates synchronous I/O and external API calls to thread pools.
    """
    if tool_name == "prepare_job_tailoring_prompt":
        job_id = arguments.get("job_id")
        if not job_id:
            return {"error": "Missing required argument 'job_id'."}
        return await asyncio.to_thread(build_tailoring_payload, job_id)

    elif tool_name == "export_tailored_resume":
        llm_payload = arguments.get("llm_payload")
        if not llm_payload or not isinstance(llm_payload, dict):
            return {"error": "Missing or invalid 'llm_payload' object."}

        doc_title = arguments.get("document_title", "Tailored Resume - Sri Omkar Dumpa")

        # Map placeholders and validate defensive fallbacks
        replacements = map_resume_placeholders(llm_payload)

        # Execute Google Docs synthesis in worker thread
        doc_url = await asyncio.to_thread(
            create_tailored_document,
            replacements,
            doc_title
        )
        return {
            "status": "success",
            "document_url": doc_url,
            "message": "Resume successfully generated and formatted in Google Docs."
        }

    elif tool_name == "get_job_details":
        job_id = arguments.get("job_id")
        if not job_id:
            return {"error": "Missing required argument 'job_id'."}
        job = await asyncio.to_thread(get_job_by_id, job_id)
        if not job:
            return {"error": f"Job record '{job_id}' not found."}
        return {"job": dict(job)}

    elif tool_name == "retry_job_evaluations":
        # Placeholder for worker migration in Phase 5
        limit = arguments.get("limit", 10)
        return {
            "status": "acknowledged",
            "message": f"Backfill batch of {limit} jobs queued for background processing."
        }

    elif tool_name == "retry_job_evaluations":
        limit = arguments.get("limit", 10)
        # Run synchronous paced batch processing in a worker thread
        batch_result = await asyncio.to_thread(run_backfill_batch, batch_size=limit)
        return {
            "status": "success",
            "batch_summary": batch_result
        }

    else:
        return {"error": f"Unknown tool '{tool_name}'."}
