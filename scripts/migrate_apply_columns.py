# scripts/migrate_apply_columns.py
import logging

from src.core.database import get_db_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

MIGRATION_SQL = """
ALTER TABLE scout.saved_jobs 
ADD COLUMN IF NOT EXISTS apply_status VARCHAR(50) DEFAULT 'IDLE',
ADD COLUMN IF NOT EXISTS review_screenshot_path TEXT NULL,
ADD COLUMN IF NOT EXISTS apply_notes TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_saved_jobs_apply_status 
ON scout.saved_jobs (apply_status);
"""


def run_migration():
    logger.info("Applying migration to scout.saved_jobs...")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
        conn.commit()
    logger.info("Migration applied successfully!")


if __name__ == "__main__":
    run_migration()
