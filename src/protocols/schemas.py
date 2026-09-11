# src/protocols/schemas.py
from typing import Dict, Any, List

MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "prepare_job_tailoring_prompt",
        "description": (
            "Fetches job specifications from PostgreSQL, applies company architectural RAG frameworks, "
            "and generates structured tailoring guidelines with dynamic phase weighting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Unique identifier of the target job record in PostgreSQL."
                }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "export_tailored_resume",
        "description": (
            "Synthesizes tailored resume content into a standardized Google Document using OAuth 2.0, "
            "enforcing deterministic profile truth and Google XYZ metric formatting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "llm_payload": {
                    "type": "object",
                    "description": "Structured JSON containing professional_summary, technical_skills, and experience bullets."
                },
                "document_title": {
                    "type": "string",
                    "description": "Optional title for the exported Google Doc. Defaults to candidate name and job title."
                }
            },
            "required": ["llm_payload"]
        }
    },
    {
        "name": "get_job_details",
        "description": "Retrieves full metadata and description for a specific job from PostgreSQL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Primary key of the job in saved_jobs."
                }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "retry_job_evaluations",
        "description": "Triggers background evaluation for jobs marked as PENDING or FAILED with API rate pacing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of records to evaluate in this batch.",
                    "default": 10
                }
            }
        }
    }
]