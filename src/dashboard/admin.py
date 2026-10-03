# src/dashboard/admin.py
"""
Remote diagnostics API for the platform operator's assistant.

Friday (the AI operator) cannot SSH into the VM, but can reach this dashboard
over HTTPS. These read-only endpoints let Friday pull logs and health state
to diagnose issues without any shell access:

    GET /api/admin/health                      service + queue health snapshot
    GET /api/admin/logs?service=mcp&tail=200   tail a service log file

Security model:
* Bearer-token auth via ADMIN_API_TOKEN (hmac.compare_digest).
* Secure by default: when ADMIN_API_TOKEN is unset the routes behave as if
  they do not exist (404), so the public dashboard is unaffected.
* Unauthenticated callers get 404, not 401, to avoid revealing the surface.
* Strictly read-only: no restarts, no shell, no writes.
"""
import hmac
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src.core.config import settings
from src.core.database import get_db_connection

logger = logging.getLogger("de-job-intelligence.admin")

# service name -> log file name under LOGS_DIR
LOG_FILES = {
    "mcp": "mcp.log",
    "dashboard": "dashboard.log",
    "apply-worker": "apply-worker.log",
}

MAX_TAIL_LINES = 2000
DEFAULT_TAIL_LINES = 200
# Never read more than this many bytes from the end of a log file.
MAX_TAIL_BYTES = 1_000_000


def _is_admin(request: Request) -> bool:
    token = settings.ADMIN_API_TOKEN
    if not token:
        return False
    auth = request.headers.get("authorization", "")
    scheme, _, credential = auth.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return False
    return hmac.compare_digest(credential, token)


def _not_found() -> Response:
    # 404 (not 401) so unauthenticated callers cannot tell the endpoint exists.
    return Response(status_code=404)


def _check_url(url: str, timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            ok = 200 <= response.status < 300
            return {"status": "ok" if ok else f"http_{response.status}", "url": url}
    except Exception as exc:
        return {"status": "unreachable", "url": url, "detail": str(exc)[:200]}


def _check_database() -> dict:
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


def _queue_depths() -> dict:
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT apply_status, COUNT(*) AS count
                FROM saved_jobs
                WHERE apply_status IS NOT NULL AND apply_status != 'IDLE'
                GROUP BY apply_status;
                """
            )
            return {row["apply_status"]: int(row["count"]) for row in cur.fetchall()}
    except Exception as exc:
        logger.warning("Admin queue-depth query failed: %s", exc)
        return {"error": str(exc)[:200]}


async def admin_health(request: Request):
    if not _is_admin(request):
        return _not_found()
    return JSONResponse(
        {
            "dashboard": "ok",
            "database": _check_database(),
            "mcp_server": _check_url(settings.MCP_HEALTH_URL),
            "queue": _queue_depths(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def _tail_lines(path: Path, n: int) -> list:
    """Returns the last n lines of a file without loading all of it."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        start = max(0, end - MAX_TAIL_BYTES)
        fh.seek(start)
        chunk = fh.read()
    if start > 0:
        # Drop the first (partial) line when we started mid-file.
        chunk = chunk.split(b"\n", 1)[-1]
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]


async def admin_logs(request: Request):
    if not _is_admin(request):
        return _not_found()

    service = request.query_params.get("service", "")
    if service not in LOG_FILES:
        return JSONResponse(
            {"error": f"Unknown service. Valid: {sorted(LOG_FILES)}"},
            status_code=400,
        )

    try:
        tail = int(request.query_params.get("tail", DEFAULT_TAIL_LINES))
    except ValueError:
        tail = DEFAULT_TAIL_LINES
    tail = max(1, min(tail, MAX_TAIL_LINES))

    path = settings.LOGS_DIR / LOG_FILES[service]
    if not path.exists():
        return JSONResponse(
            {"service": service, "path": str(path), "error": "log file not found", "lines": []},
            status_code=404,
        )

    try:
        lines = _tail_lines(path, tail)
    except Exception as exc:
        logger.warning("Admin log tail failed for %s: %s", path, exc)
        return JSONResponse({"service": service, "error": str(exc)[:200]}, status_code=500)

    return JSONResponse(
        {
            "service": service,
            "path": str(path),
            "lines_returned": len(lines),
            "lines": lines,
        }
    )


admin_routes = [
    Route("/api/admin/health", admin_health, methods=["GET"]),
    Route("/api/admin/logs", admin_logs, methods=["GET"]),
]
