# src/protocols/schemas.py
MCP_TOOLS = [
    {
        "name": "prepare_job_tailoring_prompt",
        "description": "Fetches job specifications from PostgreSQL, applies 5-phase company architectural RAG frameworks, and returns tailoring prompt guidelines with dynamic phase weighting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Unique identifier of the target job record in PostgreSQL.",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "export_tailored_resume",
        "description": "Synthesizes tailored resume content into a standardized Google Document using OAuth 2.0, enforcing the 66-token template and Google XYZ formatting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "llm_payload": {
                    "type": "object",
                    "description": "Structured JSON containing professional_summary, technical_skills, and experience bullets.",
                },
                "document_title": {
                    "type": "string",
                    "description": "Title for the exported Google Doc.",
                },
            },
            "required": ["llm_payload"],
        },
    },
    {
        "name": "get_job_details",
        "description": "Retrieves full metadata and description for a specific job from PostgreSQL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Primary key of the job in saved_jobs.",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "search_saved_jobs",
        "description": "Searches stored jobs in PostgreSQL by category, minimum match score, or status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "e.g., data_engineering, devops", "default": "data_engineering"},
                "min_score": {"type": "integer", "description": "Minimum heuristic or AI fit score", "default": 0},
                "status": {"type": "string", "description": "PENDING, PROCESSED, or FAILED", "default": "ALL"},
                "limit": {"type": "integer", "description": "Maximum records to return", "default": 10},
            },
        },
    },
    {
        "name": "retry_job_evaluations",
        "description": "Triggers background evaluation for jobs marked as PENDING or FAILED using rate-paced Gemini evaluation batches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of records to evaluate in this batch.",
                    "default": 5,
                }
            },
        },
    },
    {
    "name": "run_batch_ingestion",
    "description": "Scrapes and stages new jobs from LinkedIn/Indeed, prevents duplicates, computes zero-cost local TF-IDF match scores, and saves to PostgreSQL.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "search_term": {"type": "string", "default": "Data Engineer Spark"},
            "location": {"type": "string", "default": "Remote"},
            "results_wanted": {"type": "integer", "default": 5},
            "hours_old": {"type": "integer", "default": 48},
            "job_category": {"type": "string", "default": "data_engineering"}
        }
    }
},
]