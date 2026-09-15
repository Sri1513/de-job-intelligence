from pathlib import Path
import json
import logging
from typing import Any, Dict

from src.synthesis.gdrive_docs import generate_resume_from_llm_payload
from src.synthesis.prompt_builder import build_tailoring_prompt

logger = logging.getLogger(__name__)

# Expose prompt builder and aliases for backwards compatibility
prepare_job_for_tailoring = build_tailoring_prompt
prepare_job_tailoring_prompt = build_tailoring_prompt


def export_tailored_resume_to_drive(arguments: dict) -> dict:
    """
    Clones the master Google Doc template, executes token replacements,
    and exports the tailored resume to Google Drive.
    """
    try:
        company_name = arguments.get("company_name") or arguments.get("company", "Company")
        job_title = arguments.get("job_title") or arguments.get("title", "Data Engineer")
        doc_title = f"{company_name} - Tailored Resume - {job_title}"

        # Save debug snapshot of raw input arguments
        Path("logs").mkdir(parents=True, exist_ok=True)
        Path("logs/last_tailoring_payload.json").write_text(
            json.dumps(arguments, indent=2, default=str),
            encoding="utf-8"
        )

        raw_content = arguments.get("tailored_content")
        if raw_content is None:
            tailored_data = arguments
        elif isinstance(raw_content, str):
            tailored_data = json.loads(raw_content)
        else:
            tailored_data = raw_content

        doc_url = generate_resume_from_llm_payload(
            llm_payload=tailored_data,
            document_title=doc_title
        )

        return {
            "status": "success",
            "document_url": doc_url,
            "message": f"Successfully exported tailored resume for {company_name}."
        }
    except Exception as e:
        logger.error(f"Failed to export resume to Google Drive: {e}")
        return {"status": "error", "message": str(e)}


# Dispatcher alias
export_tailored_resume = export_tailored_resume_to_drive
