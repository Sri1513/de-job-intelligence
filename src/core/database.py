# src/core/database.py
from contextlib import contextmanager
from typing import Any  # <--- Ensure this import is present

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from src.core.config import settings

DATABASE_URL = (
    f"postgresql://{settings.DB_USER}:{settings.DB_PASSWORD}@"
    f"{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Lazily initializes and returns the shared database connection pool."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=True)
    return _pool


@contextmanager
def get_db_connection():
    """Context manager for acquiring a connection from the pool."""
    pool = get_pool()
    with pool.connection() as conn:
        conn.row_factory = dict_row
        yield conn


# In src/core/database.py
def get_job_by_id(job_id: str) -> dict[str, Any] | None:
    """Fetches full job details including application review metadata."""
    query = """
        SELECT 
            job_id,
            title,
            company,
            location,
            is_remote,
            description,
            fit_score AS match_score,
            ai_status,
            notes AS ai_notes,
            saved_at AS created_at,
            job_category,
            job_url,
            apply_status,
            applied_at,
            review_screenshot_path,
            apply_notes
        FROM saved_jobs
        WHERE job_id = %s;
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None
