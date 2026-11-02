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
_reader_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Lazily initializes and returns the shared database connection pool."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=True)
    return _pool


def get_reader_pool() -> ConnectionPool:
    """Read-only pool for the public MCP surface (src/protocols/dashboard_mcp).

    Connects as mcp_dashboard_reader (migrations 004/005), which holds
    SELECT on exactly three sanitized views and nothing else. The password
    comes from MCP_DASHBOARD_READER_PASSWORD in .env and is never logged
    or committed. max_size stays below the role's CONNECTION LIMIT 5 so
    one manual psql session can always still connect. Fail-closed: raises
    instead of returning a pool when the password is not configured.
    """
    global _reader_pool
    if _reader_pool is None:
        password = settings.MCP_DASHBOARD_READER_PASSWORD
        if not password:
            raise RuntimeError(
                "MCP_DASHBOARD_READER_PASSWORD is not set. The public MCP "
                "route refuses to serve data without it."
            )
        conninfo = (
            f"postgresql://mcp_dashboard_reader:{password}@"
            f"{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
        )
        _reader_pool = ConnectionPool(
            conninfo=conninfo, min_size=1, max_size=4, open=True
        )
    return _reader_pool


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
