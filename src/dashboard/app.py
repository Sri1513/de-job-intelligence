# src/dashboard/app.py
import json
import logging
from datetime import datetime, date
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

async def api_jobs(request: Request) -> JSONResponse:
    """
    Returns the complete list of jobs for the selected category.
    Includes parsed metadata, missing skills, and intelligent date calculations.
    """
    category = request.query_params.get("category", "data_engineering")

    query = """
        SELECT job_id, title, company, location, employment_type, sponsorship, 
               fit_score, status, metadata, job_url, saved_at, notes
        FROM saved_jobs 
        WHERE job_category ILIKE %s 
        ORDER BY saved_at DESC NULLS LAST;
    """

    jobs = []
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (f"%{category}%",))
                rows = cur.fetchall()

                for row in rows:
                    meta = row.get("metadata") or {}
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except Exception:
                            meta = {}

                    # Intelligent Date Fallback: metadata -> saved_at timestamp -> 'Recently'
                    date_val = meta.get("date_posted")
                    if not date_val or date_val == "N/A":
                        if row.get("saved_at"):
                            date_val = str(row["saved_at"]).split(" ")[0]
                        else:
                            date_val = "Recently"

                    jobs.append({
                        "job_id": row.get("job_id") or "N/A",
                        "title": row.get("title") or "Untitled",
                        "company": row.get("company") or "Unknown Company",
                        "location": row.get("location") or "Remote",
                        "employment_type": row.get("employment_type") or "Unknown",
                        "sponsorship": row.get("sponsorship") or "Not Mentioned",
                        "fit_score": row.get("fit_score") or "0",
                        "status": row.get("status") or "saved",
                        "metadata": serialize_for_json(meta),
                        "date_posted": date_val,
                        "job_url": row.get("job_url") or "#",
                        "saved_at": serialize_for_json(row.get("saved_at")),
                        "notes": row.get("notes") or "No AI notes generated."
                    })
    except Exception as exc:
        logger.error(f"Error querying /api/jobs: {exc}", exc_info=True)
        return JSONResponse([], status_code=500)

    # Return the clean array expected by dashboard.html
    return JSONResponse(jobs)

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
        with get_db_connection() as conn:
            with conn.cursor() as cur:
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

routes = [
    Route("/", dashboard_view, methods=["GET"]),
    Route("/api/jobs", api_jobs, methods=["GET"]),
    Route("/audit", audit_view, methods=["GET"])
]

app = Starlette(debug=True, routes=routes)