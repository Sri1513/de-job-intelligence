# src/core/database.py
from contextlib import contextmanager

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
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=10,
            open=True
        )
    return _pool

@contextmanager
def get_db_connection():
    """Context manager for acquiring a connection from the pool."""
    pool = get_pool()
    with pool.connection() as conn:
        conn.row_factory = dict_row
        yield conn

def get_job_by_id(job_id: str) -> dict | None:
    """Fetch a single job record by its primary key."""
    query = """
        SELECT
            job_id, title, company, location, is_remote, job_url,
            description, job_category,
            fit_score AS match_score,
            notes AS ai_notes,
            ai_status,
            saved_at AS created_at
        FROM saved_jobs
        WHERE job_id = %s;
    """
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, (job_id,))
        return cur.fetchone()
