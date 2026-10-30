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
                "category": {
                    "type": "string",
                    "description": "e.g., data_engineering, devops",
                    "default": "data_engineering",
                },
                "min_score": {
                    "type": "integer",
                    "description": "Minimum heuristic or AI fit score",
                    "default": 0,
                },
                "status": {
                    "type": "string",
                    "description": "PENDING, PROCESSED, or FAILED",
                    "default": "ALL",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return",
                    "default": 10,
                },
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
                "job_category": {"type": "string", "default": "data_engineering"},
                "c2c_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, restrict to Corp-to-Corp arrangements: JobSpy requests contract roles only, and after description enrichment only rows whose title/description confirms C2C are staged.",
                },
            },
        },
    },
    {
        "name": "stage_agent_jobs",
        "description": (
            "Stage jobs found by browsing a job portal as the user into the main dashboard feed. "
            "Use AFTER you have read the full posting pages yourself (LinkedIn, Indeed, Dice, or a "
            "company career page) at human pace in a logged-in session, so the portal's own filters "
            "actually apply. Pass one object per posting with its title, company, posting URL, "
            "location, portal, and full description text. Rows are deduplicated against existing "
            "jobs (same LinkedIn ID, or same company+title+URL), scored locally at zero API cost, "
            "and appear in the main dashboard feed under the given job category — there is no "
            "separate board. Any extra detail fields you captured (workplace, duration, rate text, "
            "poster/recruiter, apply method) are preserved into the row's metadata. "
            "Rows that already exist are refreshed in place instead of duplicated: their "
            "job_category moves to the given category, your detail fields merge into metadata "
            "(pipeline-computed locations, source, and fit scores are never overwritten), and "
            "the sponsorship column updates when you supplied one. "
            "Set run_evaluation=true (default) to also run the Groq-primary AI evaluation on the "
            "newly staged, refreshed, and already-present jobs still pending evaluation; for batches over ~25 jobs, pass run_evaluation=false and call "
            "retry_job_evaluations afterwards to stay inside client stream timeouts. "
            "Do NOT use this tool for scraped/JobSpy rows — that path is run_batch_ingestion, "
            "unchanged. Do not run two staging calls at once for the same jobs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "description": (
                        "Postings you read yourself, at most 100 per call. Each needs title, "
                        "company, and job_url (the full posting URL — required for dedupe and the "
                        "dashboard link); location, source (portal name, e.g. linkedin, indeed, "
                        "dice), and the full description text are strongly recommended."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "company": {"type": "string"},
                            "job_url": {"type": "string"},
                            "location": {"type": "string"},
                            "source": {"type": "string"},
                            "description": {"type": "string"},
                            "employment_type": {"type": "string"},
                            "sponsorship": {"type": "string"},
                            "date_posted": {"type": "string"},
                        },
                        "required": ["title", "company", "job_url"],
                    },
                },
                "job_category": {
                    "type": "string",
                    "description": "Dashboard category the rows appear under (e.g. data_engineering).",
                    "default": "data_engineering",
                },
                "run_evaluation": {
                    "type": "boolean",
                    "description": "Also AI-evaluate just the newly staged jobs (Groq primary, Gemini fallback).",
                    "default": True,
                },
                "default_source": {
                    "type": "string",
                    "description": "Source label for postings that name no portal.",
                    "default": "Agent",
                },
            },
            "required": ["jobs"],
        },
    },
    {
        "name": "process_job_alert_draft",
        "description": "Parses a WhatsApp job alert snippet, registers or looks up the helper, tailors/generates the resume into 'Resumes and Cover Letters/<Company>/Sri Omkar - Data Engineer', creates a dynamic Gmail draft with the helper in CC, and logs everything to PostgreSQL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "whatsapp_text": {
                    "type": "string",
                    "description": "The raw text snippet copied from WhatsApp.",
                },
                "helper_name": {
                    "type": "string",
                    "description": "The name of the helper or referrer.",
                },
                "helper_email": {
                    "type": "string",
                    "description": "Email of the helper (required if new helper).",
                },
                "helper_company": {
                    "type": "string",
                    "description": "Company where the helper works.",
                },
            },
            "required": ["whatsapp_text", "helper_name"],
        },
    },
    # src/protocols/schemas.py (Add this entry to MCP_TOOLS)
    {
        "name": "run_email_pipeline",
        "description": "Pulls recent LinkedIn job alerts from Gmail, widens metadata via JobSpy scraper, stages raw jobs with deduplication, and triggers Gemini AI evaluation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of email alerts to fetch from inbox.",
                    "default": 5,
                },
                "job_category": {
                    "type": "string",
                    "description": "Target job category (e.g., data_engineering, devops).",
                    "default": "data_engineering",
                },
            },
        },
    },
    # Add to the tools manifest list in src/protocols/schemas.py:
    {
        "name": "queue_job_for_application",
        "description": "Flags a saved job as QUEUED for autonomous browser-based autofill.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The unique identifier of the job in saved_jobs.",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "run_application_worker",
        "description": "Executes the browser agent to autofill queued job applications and gate them for human review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of queued jobs to process in this batch (default: 3).",
                },
                "headless": {
                    "type": "boolean",
                    "description": "Whether to run browser in headless mode (default: true).",
                },
            },
        },
    },
]
