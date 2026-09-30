# src/database/helper_repo.py
import logging
from typing import Any, Dict, Optional

from src.core.database import get_db_connection

logger = logging.getLogger("de-job-intelligence.database")


def get_or_create_helper(
    name: str,
    email: Optional[str] = None,
    company: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Retrieves an existing helper by name/email or inserts a new one into scout.helpers.
    Returns the helper_id.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if email:
                cur.execute("SELECT helper_id FROM scout.helpers WHERE email = %s;", (email,))
            else:
                cur.execute("SELECT helper_id FROM scout.helpers WHERE name = %s;", (name,))
            row = cur.fetchone()

            if row:
                helper_id = row["helper_id"] if isinstance(row, dict) else row[0]
                cur.execute(
                    """
                    UPDATE scout.helpers 
                    SET name = COALESCE(%s, name), 
                        company = COALESCE(%s, company),
                        metadata = COALESCE(%s, metadata)
                    WHERE helper_id = %s;
                    """,
                    (name, company, json_str(metadata), helper_id),
                )
                return helper_id
            else:
                cur.execute(
                    """
                    INSERT INTO scout.helpers (name, email, company, metadata)
                    VALUES (%s, %s, %s, %s)
                    RETURNING helper_id;
                    """,
                    (name, email, company, json_str(metadata)),
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
    short_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Implements true CDC Type 2 using database versioning columns:
    - Finds the current active record (`is_current = TRUE`) for the domain `short_id`.
    - Expires it (`is_current = FALSE`, sets `effective_end`).
    - Inserts a new row with an incremented `version`, `is_current = TRUE`, and new `effective_start`.
    """
    import json
    from datetime import datetime

    metadata = metadata or {}
    short_id = short_id or "unkx"
    entity_key = f"domain_{short_id}"
    now = datetime.now()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # 1. Find the current active record for this domain short_id
            cur.execute(
                """
                SELECT outreach_id, version 
                FROM scout.outreach_tracking 
                WHERE short_id = %s AND is_current = TRUE;
                """,
                (short_id,),
            )
            existing = cur.fetchone()

            new_version = 1
            if existing:
                old_id = existing["outreach_id"] if isinstance(existing, dict) else existing[0]
                old_version = existing["version"] if isinstance(existing, dict) else existing[1]
                new_version = old_version + 1

                # 2. CDC Type 2: Expire the old active record
                cur.execute(
                    """
                    UPDATE scout.outreach_tracking
                    SET is_current = FALSE, effective_end = %s
                    WHERE outreach_id = %s;
                    """,
                    (now, old_id),
                )

            # 3. Insert the new active version row
            cur.execute(
                """
                INSERT INTO scout.outreach_tracking (
                    helper_id, company_name, job_title, extracted_jd, 
                    resume_doc_url, gmail_draft_id, recruiter_name, 
                    short_id, entity_key, version, is_current, effective_start, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)
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
                    short_id,
                    entity_key,
                    new_version,
                    now,
                    json.dumps(metadata),
                ),
            )
            row = cur.fetchone()
            outreach_id = row["outreach_id"] if isinstance(row, dict) else row[0]
            conn.commit()
            logger.info(
                f"CDC Type 2: Created version {new_version} (ID: {outreach_id}) for domain code '{short_id}'"
            )
            return outreach_id


def json_str(data: Optional[Dict[str, Any]]) -> Optional[str]:
    import json

    if data is None:
        return None
    return json.dumps(data)
