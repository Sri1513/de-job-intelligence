# src/dashboard/app.py
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from src.core.database import get_db_connection

logger = logging.getLogger("de-job-intelligence.dashboard")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

def serialize_for_json(obj):
    """Recursively serializes timestamps and decimals for JSON transport."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: serialize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [serialize_for_json(i) for i in obj]
    return obj

async def dashboard_view(request: Request) -> HTMLResponse:
    """Renders the main dashboard shell."""
    category = request.query_params.get("category", "data_engineering")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"current_category": category}
    )

# Add to src/dashboard/app.py

async def api_jobs(request: Request) -> JSONResponse:
    """Returns jobs with isolated AI evaluation notes and metadata personal notes."""
    category = request.query_params.get("category", "data_engineering")

    query = """
        SELECT job_id, title, company, location, employment_type, sponsorship,
               fit_score, status, metadata, job_url, saved_at, notes, applied_at
        FROM saved_jobs
        WHERE job_category ILIKE %s
        ORDER BY saved_at DESC NULLS LAST;
    """

    jobs = []
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(query, (f"%{category}%",))
            rows = cur.fetchall()

            for row in rows:
                meta = row.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}

                date_val = meta.get("date_posted")
                if not date_val or date_val == "N/A":
                    date_val = str(row.get("saved_at")).split(" ")[0] if row.get("saved_at") else "Recently"

                # AI evaluation notes remain untouched from the DB notes column
                ai_notes = row.get("notes") or "No AI notes generated."
                # Personal notes are fetched strictly from metadata JSON
                personal_notes = meta.get("personal_notes", "")

                jobs.append({
                    "job_id": row.get("job_id") or "N/A",
                    "title": row.get("title") or "Untitled",
                    "company": row.get("company") or "Unknown Company",
                    "location": row.get("location") or "Remote",
                    "employment_type": row.get("employment_type") or "Unknown",
                    "sponsorship": row.get("sponsorship") or "Not Mentioned",
                    "fit_score": row.get("fit_score") or "0",
                    "status": row.get("status") or "saved",
                    "applied_at": serialize_for_json(row.get("applied_at")),
                    "metadata": serialize_for_json(meta),
                    "date_posted": date_val,
                    "job_url": row.get("job_url") or "#",
                    "saved_at": serialize_for_json(row.get("saved_at")),
                    "ai_notes": ai_notes,
                    "personal_notes": personal_notes
                })
    except Exception as exc:
        logger.error(f"Error querying /api/jobs: {exc}", exc_info=True)
        return JSONResponse([], status_code=500)

    return JSONResponse(jobs)

async def api_update_job_notes(request: Request) -> JSONResponse:
    """Updates ONLY the personal_notes key inside the metadata JSON column."""
    try:
        data = await request.json()
        job_id = data.get("job_id")
        new_personal_notes = data.get("notes", "")

        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT metadata FROM saved_jobs WHERE job_id = %s;", (job_id,))
            row = cur.fetchone()
            if not row:
                return JSONResponse({"status": "error", "message": "Job not found"}, status_code=404)

            meta = row.get("metadata") if isinstance(row, dict) else row[0]
            if isinstance(meta, str):
                try: 
                    meta = json.loads(meta)
                except Exception: 
                    meta = {}
            elif not isinstance(meta, dict):
                meta = {}

            # Inject/Update personal_notes inside metadata without touching anything else
            meta["personal_notes"] = new_personal_notes

            cur.execute(
                "UPDATE saved_jobs SET metadata = %s WHERE job_id = %s;",
                (json.dumps(meta), job_id)
            )
            conn.commit()

        return JSONResponse({"status": "success", "message": f"Updated metadata personal notes for {job_id}"})
    except Exception as exc:
        logger.error(f"Error updating metadata personal notes: {exc}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)

async def api_update_job_status(request: Request) -> JSONResponse:
    """Updates job status to 'applied' and records current timestamp in applied_at."""
    try:
        data = await request.json()
        job_id = data.get("job_id")
        new_status = data.get("status", "applied")

        with get_db_connection() as conn, conn.cursor() as cur:
            if new_status.lower() == 'applied':
                cur.execute(
                    """
                    UPDATE saved_jobs 
                    SET status = %s, applied_at = CURRENT_TIMESTAMP 
                    WHERE job_id = %s;
                    """,
                    (new_status, job_id)
                )
            else:
                cur.execute(
                    """
                    UPDATE saved_jobs 
                    SET status = %s, applied_at = NULL 
                    WHERE job_id = %s;
                    """,
                    (new_status, job_id)
                )
            conn.commit()
        return JSONResponse({"status": "success", "message": f"Updated job {job_id} to '{new_status}'"})
    except Exception as exc:
        logger.error(f"Error updating job status: {exc}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)

async def audit_view(request: Request) -> HTMLResponse:
    """
    Renders the dedicated Skill Audit & Gap Analysis view
    showing matched, missing, and AI-extracted technologies.
    """
    category = request.query_params.get("category", "data_engineering")

    query = """
        SELECT title, company, job_category, metadata, job_url, saved_at
        FROM saved_jobs
        WHERE job_category ILIKE %s
        ORDER BY saved_at DESC NULLS LAST;
    """

    audit_jobs = []
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(query, (f"%{category}%",))
            rows = cur.fetchall()

            for row in rows:
                meta = row.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}

                audit_jobs.append({
                    "title": row.get("title") or "Untitled",
                    "company": row.get("company") or "Unknown Company",
                    "job_category": row.get("job_category") or category,
                    "matched_skills": meta.get("matched_skills", []),
                    "missing_skills": meta.get("missing_skills", []),
                    "ai_extracted_skills": meta.get("ai_extracted_skills", []),
                    "job_url": row.get("job_url") or "#",
                    "saved_at": serialize_for_json(row.get("saved_at"))
                })
    except Exception as exc:
        logger.error(f"Error querying /audit: {exc}", exc_info=True)

    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "audit_jobs": audit_jobs,
            "current_category": category
        }
    )

# Add these functions to src/dashboard/app.py

async def whatsapp_alerts_view(request: Request) -> HTMLResponse:
    """Renders the dedicated WhatsApp Job Alerts dashboard view."""
    return templates.TemplateResponse(request, "whatsapp_alerts.html", {})

# Update api_whatsapp_alerts in src/dashboard/app.py
async def api_whatsapp_alerts(request: Request) -> JSONResponse:
    """Returns active WhatsApp outreach tracking records with robust metadata extraction."""
    query = """
        SELECT o.outreach_id, o.short_id, o.company_name, o.job_title, 
               o.recruiter_name, o.status, o.created_at, o.resume_doc_url, 
               o.gmail_draft_id, o.metadata, h.name as helper_name, h.email as helper_email
        FROM scout.outreach_tracking o
        LEFT JOIN scout.helpers h ON o.helper_id = h.helper_id
        WHERE o.is_current = TRUE
        ORDER BY o.created_at DESC;
    """
    alerts = []
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
            for row in rows:
                # Robust metadata handling for dict, string, or None
                raw_meta = row.get("metadata")
                if isinstance(raw_meta, str):
                    try:
                        meta = json.loads(raw_meta)
                    except Exception:
                        meta = {}
                elif isinstance(raw_meta, dict):
                    meta = raw_meta
                else:
                    meta = {}
                
                whatsapp_message = meta.get("whatsapp_raw") or meta.get("whatsapp_text") or "No raw message recorded."
                
                alerts.append({
                    "outreach_id": row.get("outreach_id"),
                    "short_id": row.get("short_id") or "unkx",
                    "company_name": row.get("company_name") or "Unknown",
                    "job_title": row.get("job_title") or "Data Engineer",
                    "recruiter_name": row.get("recruiter_name") or "Hiring Team",
                    "status": row.get("status") or "Drafted",
                    "created_at": serialize_for_json(row.get("created_at")),
                    "resume_doc_url": row.get("resume_doc_url") or "#",
                    "gmail_draft_id": row.get("gmail_draft_id") or "",
                    "helper_name": row.get("helper_name") or "Unknown",
                    "helper_email": row.get("helper_email") or "",
                    "recipient_email": meta.get("recipient_email") or "N/A",
                    "whatsapp_raw": whatsapp_message
                })
    except Exception as exc:
        logger.error(f"Error fetching WhatsApp alerts: {exc}", exc_info=True)
        return JSONResponse([], status_code=500)
    
    return JSONResponse(alerts)

async def api_update_outreach_status(request: Request) -> JSONResponse:
    """Updates the status of an outreach record (e.g., to 'Sent')."""
    try:
        data = await request.json()
        outreach_id = data.get("outreach_id")
        new_status = data.get("status", "Sent")

        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE scout.outreach_tracking SET status = %s WHERE outreach_id = %s;",
                (new_status, outreach_id)
            )
            conn.commit()
        return JSONResponse({"status": "success", "message": f"Updated outreach ID {outreach_id} to '{new_status}'"})
    except Exception as exc:
        logger.error(f"Error updating outreach status: {exc}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)

# Add to src/dashboard/app.py

async def skills_analytics_view(request: Request) -> HTMLResponse:
    """Renders the Top Skills Demographics & Analytics dashboard view."""
    category = request.query_params.get("category", "data_engineering")
    return templates.TemplateResponse(request, "skills_analytics.html", {"current_category": category})

async def api_skills_demographics(request: Request) -> JSONResponse:
    """Aggregates skill frequencies, normalizes naming variants, and supports dynamic limits."""
    category = request.query_params.get("category", "data_engineering")
    limit_param = request.query_params.get("limit", "10")
    
    query = "SELECT metadata FROM saved_jobs WHERE job_category ILIKE %s;"
    
    # Comprehensive skill aliases and normalization mapping
    skill_aliases = {
        "spark": "Apache Spark",
        "apache spark": "Apache Spark",
        "spark sql": "Apache Spark",
        "pyspark": "PySpark",
        "airflow": "Apache Airflow",
        "apache airflow": "Apache Airflow",
        "aws": "AWS",
        "amazon web services": "AWS",
        "gcp": "GCP",
        "google cloud platform": "GCP",
        "azure": "Azure",
        "microsoft azure": "Azure",
        "sql": "SQL",
        "python": "Python",
        "dbt": "dbt",
        "ci/cd": "CI/CD",
        "cicd": "CI/CD",
        "postgresql": "PostgreSQL",
        "postgres": "PostgreSQL",
        "power bi": "Power BI",
        "aws glue": "AWS Glue"
    }

    def normalize_skill(raw_skill: str) -> str:
        cleaned = raw_skill.strip().lower()
        return skill_aliases.get(cleaned, raw_skill.strip().title())

    skill_counts = {}
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(query, (f"%{category}%",))
            rows = cur.fetchall()
            for row in rows:
                meta = row.get("metadata") or {}
                if isinstance(meta, str):
                    try: 
                        meta = json.loads(meta)
                    except Exception: 
                        meta = {}
                
                skills = meta.get("ai_extracted_skills") or []
                for skill in skills:
                    canonical_skill = normalize_skill(skill)
                    skill_counts[canonical_skill] = skill_counts.get(canonical_skill, 0) + 1
                    
        # Sort descending by frequency
        sorted_skills = sorted(skill_counts.items(), key=lambda x: x[1], reverse=True)
        
        # Apply limit if specified and not 'all'
        if limit_param != "all":
            try:
                limit_val = int(limit_param)
                sorted_skills = sorted_skills[:limit_val]
            except ValueError:
                sorted_skills = sorted_skills[:10]
        
        labels = [item[0] for item in sorted_skills]
        counts = [item[1] for item in sorted_skills]
        
        return JSONResponse({"labels": labels, "counts": counts})
    except Exception as exc:
        logger.error(f"Error computing skills demographics: {exc}", exc_info=True)
        return JSONResponse({"labels": [], "counts": []}, status_code=500)

routes = [
    Route("/", dashboard_view, methods=["GET"]),
    Route("/api/jobs", api_jobs, methods=["GET"]),
    Route("/api/jobs/update-status", api_update_job_status, methods=["POST"]), # <--- Add this
    Route("/api/jobs/update-notes", api_update_job_notes, methods=["POST"]), # <--- Add this route
    Route("/audit", audit_view, methods=["GET"]),
    Route("/whatsapp-alerts", whatsapp_alerts_view, methods=["GET"]),
    Route("/api/whatsapp-alerts", api_whatsapp_alerts, methods=["GET"]),
    Route("/api/whatsapp-alerts/update-status", api_update_outreach_status, methods=["POST"]),
    Route("/skills-analytics", skills_analytics_view, methods=["GET"]),       # <--- Added
    Route("/api/skills-demographics", api_skills_demographics, methods=["GET"]) # <--- Added

]

app = Starlette(debug=True, routes=routes)
