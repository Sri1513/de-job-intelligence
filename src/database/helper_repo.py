# src/database/helper_repo.py
import logging
from typing import Optional, Dict, Any
from src.core.database import get_db_connection

logger = logging.getLogger("de-job-intelligence.database")

def get_or_create_helper(
    name: str,
    email: Optional[str] = None,
    company: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> int:
    """
    Retrieves an existing helper by name/email or inserts a new one into scout.helpers.
    Returns the helper_id.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Check if helper exists by email or name
            if email:
                cur.execute(
                    "SELECT helper_id FROM scout.helpers WHERE email = %s;",
                    (email,)
                )
            else:
                cur.execute(
                    "SELECT helper_id FROM scout.helpers WHERE name = %s;",
                    (name,)
                )
            row = cur.fetchone()

            if row:
                helper_id = row["helper_id"] if isinstance(row, dict) else row[0]
                # Update info if provided
                cur.execute(
                    """
                    UPDATE scout.helpers 
                    SET name = COALESCE(%s, name), 
                        company = COALESCE(%s, company),
                        metadata = COALESCE(%s, metadata)
                    WHERE helper_id = %s;
                    """,
                    (name, company, json_str(metadata), helper_id)
                )
                return helper_id
            else:
                cur.execute(
                    """
                    INSERT INTO scout.helpers (name, email, company, metadata)
                    VALUES (%s, %s, %s, %s)
                    RETURNING helper_id;
                    """,
                    (name, email, company, json_str(metadata))
                )
                new_row = cur.fetchone()
                helper_id = new_row["helper_id"] if isinstance(new_row, dict) else new_row[0]
                conn.commit()
                return helper_id


def log_outreach_event(
    helper_id: int,
    company_name: str,
    job_title: str,
    extracted_jd: str,
    resume_doc_url: str,
    gmail_draft_id: str,
    recruiter_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> int:
    """
    Logs an outreach event into scout.outreach_tracking, including the recruiter name.
    Returns the outreach_id.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scout.outreach_tracking (
                    helper_id, company_name, job_title, extracted_jd, 
                    resume_doc_url, gmail_draft_id, recruiter_name, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING outreach_id;
                """,
                (
                    helper_id,
                    company_name,
                    job_title,
                    extracted_jd,
                    resume_doc_url,
                    gmail_draft_id,
                    recruiter_name,
                    json_str(metadata)
                )
            )
            row = cur.fetchone()
            outreach_id = row["outreach_id"] if isinstance(row, dict) else row[0]
            conn.commit()
            logger.info(f"Logged outreach event ID {outrences_id if 'outrences_id' in locals() else outreach_id} for {company_name}")
            return outreach_id


def json_str(data: Optional[Dict[str, Any]]) -> Optional[str]:
    import json
    if data is None:
        return None
    return json.dumps(data)
