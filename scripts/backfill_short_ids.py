# scripts/backfill_short_ids.py
import json
import logging
import re
from typing import Optional

from src.core.database import get_db_connection

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("de-job-intelligence.backfill")


def generate_domain_short_id(email: Optional[str], company_name: Optional[str] = None) -> str:
    """
    Extracts the domain after '@' from an email (e.g., 'hr@databricks.com' -> 'data'),
    and returns a clean 4-character code. Falls back to company name slug if no email.
    """
    domain_part = ""
    if email and "@" in email:
        domain_part = email.split("@")[1].strip().lower()
    elif company_name:
        domain_part = re.sub(r"[^a-z0-9]", "", company_name.lower()) + ".com"
    else:
        domain_part = "unknown.com"

    parts = domain_part.split(".")
    root_name = parts[-2] if len(parts) >= 2 else parts[0]

    clean_name = re.sub(r"[^a-z0-9]", "", root_name)
    return clean_name[:4].ljust(4, "x")


def backfill_existing_outreach_records():
    """
    Scans existing rows in scout.outreach_tracking, generates domain short IDs,
    updates metadata, and populates the short_id column.
    """
    logger.info("Starting backfill for existing outreach records...")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Fetch all existing records
            cur.execute("SELECT outreach_id, company_name, metadata FROM scout.outreach_tracking;")
            rows = cur.fetchall()

            if not rows:
                logger.info("No records found in scout.outreach_tracking to backfill.")
                return

            logger.info(f"Found {len(rows)} records to evaluate.")
            updated_count = 0

            for row in rows:
                # Handle row format depending on connection cursor type (dict or tuple)
                if isinstance(row, dict):
                    outreach_id = row["outreach_id"]
                    company_name = row["company_name"]
                    metadata = row["metadata"] or {}
                else:
                    outreach_id = row[0]
                    company_name = row[1]
                    metadata = row[2] or {}

                # Ensure metadata is a dictionary if stored as a string
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        metadata = {}

                # Extract recipient email from metadata or fallback
                recipient_email = metadata.get("recipient_email")

                # Generate the 4-character domain short ID
                short_id = generate_domain_short_id(recipient_email, company_name)

                # Attach short_id inside metadata dictionary as well for redundancy
                metadata["short_id"] = short_id

                # Update the database row with the new short_id and updated metadata
                cur.execute(
                    """
                    UPDATE scout.outreach_tracking
                    SET short_id = %s, metadata = %s
                    WHERE outreach_id = %s;
                    """,
                    (short_id, json.dumps(metadata), outreach_id),
                )
                updated_count += 1
                logger.debug(
                    f"Updated outreach_id {outreach_id} with short_id '{short_id}' (Company: {company_name})"
                )

            conn.commit()
            logger.info(f"Successfully backfilled short_ids for {updated_count} existing records.")


if __name__ == "__main__":
    backfill_existing_outreach_records()
