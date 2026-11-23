# src/workers/deadline_queue_check.py
"""Daily deadline queue + dream-job advance notification.

The separate deadline queue (owner decision 2026-11-23: a separate list,
not a new status in the tracker). A job enters the queue once its derived
deadline (posted_at + 7 days) has passed and it is still apply_status
IDLE. The daily run re-checks every queued job and records the outcome;
when the job that WAS the current dream job is determined closed or
rejected, the pointer advances (GET /api/lockscreen/current picks the
next job automatically — its ordering rule only returns jobs whose
deadline has not passed) and a WhatsApp advance notification goes out
naming the old job, why it left, and the new current job.

Outcomes — only VERIFIED ones are ever recorded; nothing is invented:
  closed      posting verified gone (HTTP 404/410, or closed markers on
              a successfully fetched page)
  rejected    the engine tracker says so (metadata.tracker stage/outcome)
  applied     apply_status moved to SUBMITTED/MANUAL
  still_open  posting verified live past its deadline — stays 'tracking',
              checked again tomorrow
Fail-open: a network error, timeout, bot-block (403/429), 5xx, or an
empty/JS-shell page leaves the row untouched except for last_checked_at
— an unreachable check NEVER marks a job closed. A job that merely
expired (deadline passed, still live) advances the pointer silently:
only a verified closed/rejected determination notifies.
Re-runs never re-notify: a departed job is already 'done' and no longer
current, so the before/after comparison cannot fire twice for it.

Daily trigger — cron on the VM (ubuntu's crontab). The module only
exists inside the dashboard container after the branch merges and CI
deploys, so the cron runs the deployed code via exec (same pattern as
the manual migration runs):

    0 9 * * * cd /opt/projects/de-job-intelligence && docker compose exec -T dashboard python -m src.workers.deadline_queue_check >> logs/deadline-queue.log 2>&1

Deploy order: run migrations/010_deadline_queue.sql on the VM (manual
psql, like 009) BEFORE deploying this branch — the worker's INSERT
references deadline_queue, so code-first would break the daily check.

Runnable as a script: python -m src.workers.deadline_queue_check
"""
import json
import logging
import re
from typing import Any, Callable, Optional

import requests

from src.core.database import get_db_connection
from src.dashboard.app import LOCKSCREEN_CURRENT_QUERY
from src.engine.llm_text import generate_text
from src.engine.notifier import send_whatsapp

logger = logging.getLogger("de-job-intelligence.deadline_queue")

DEADLINE_DAYS = 7

STATE_TRACKING = "tracking"
STATE_DONE = "done"

OUTCOME_CLOSED = "closed"
OUTCOME_REJECTED = "rejected"
OUTCOME_APPLIED = "applied"
OUTCOME_STILL_OPEN = "still_open"

# Terminal outcomes: the row stops being checked daily.
TERMINAL_OUTCOMES = (OUTCOME_CLOSED, OUTCOME_REJECTED, OUTCOME_APPLIED)

# apply_status values meaning "he applied" (SUBMITTED is engine
# submissions; MANUAL is the dashboard "Mark Applied" button).
APPLIED_STATUSES = ("SUBMITTED", "MANUAL")

ENQUEUE_SQL = """
    INSERT INTO scout.deadline_queue (job_id, deadline)
    SELECT job_id, posted_at + 7
    FROM scout.saved_jobs
    WHERE apply_status = 'IDLE'
      AND posted_at IS NOT NULL
      AND posted_at + 7 < CURRENT_DATE
      AND NOT EXISTS (
          SELECT 1 FROM scout.deadline_queue dq
          WHERE dq.job_id = saved_jobs.job_id
      )
    ON CONFLICT (job_id) DO NOTHING
    RETURNING job_id;
"""

TRACKING_SQL = """
    SELECT dq.job_id, dq.deadline, s.title, s.company, s.job_url,
           s.apply_status, s.metadata
    FROM scout.deadline_queue dq
    JOIN scout.saved_jobs s ON s.job_id = dq.job_id
    WHERE dq.state = 'tracking'
    ORDER BY dq.deadline ASC;
"""

UPDATE_QUEUE_SQL = """
    UPDATE scout.deadline_queue
    SET state = %s, outcome = %s, last_checked_at = NOW()
    WHERE job_id = %s;
"""

# Fail-open touch: the check ran but could not tell — record the attempt,
# leave state and outcome exactly as they were.
TOUCH_QUEUE_SQL = """
    UPDATE scout.deadline_queue
    SET last_checked_at = NOW()
    WHERE job_id = %s;
"""

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Phrases a posting page shows when the role is gone. Matched against
# tag-stripped, whitespace-collapsed page text (lowercased). Kept to
# explicit "gone" phrasings — a bare "expired" would false-positive on
# pages that merely mention expiry.
_CLOSED_MARKERS = (
    "no longer available",
    "no longer accepting applications",
    "no longer accepting",
    "this job is no longer",
    "this position is no longer",
    "job posting is no longer",
    "posting is no longer available",
    "job has expired",
    "posting has expired",
    "job posting has expired",
    "position has been filled",
)

# Why the old job left, in the notification's words.
_REASON_TEXT = {
    OUTCOME_CLOSED: "the posting closed",
    OUTCOME_REJECTED: "the application was rejected",
}


def _default_fetcher(url: str) -> tuple[int, str]:
    """Fetches a posting URL like a browser. Returns (status, body)."""
    resp = requests.get(
        url, headers={"User-Agent": _BROWSER_UA}, timeout=15
    )
    return resp.status_code, resp.text


def _visible_text(html: str | None) -> str:
    """Tag-stripped, whitespace-collapsed page text for marker matching."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def check_posting_live(
    job_url: str | None,
    fetcher: Optional[Callable[[str], tuple[int, str]]] = None,
) -> str:
    """'live' | 'closed' | 'unknown' for one posting URL.

    Fail-open: only a definitive signal reads 'closed' (HTTP 404/410,
    or closed markers on a readable page). Network errors, timeouts,
    bot-blocks (403/429), 5xx, and empty/JS-shell pages are 'unknown' —
    never 'closed'. A readable page with no markers is 'live'.
    """
    if not job_url:
        return "unknown"
    fetch = fetcher or _default_fetcher
    try:
        status, body = fetch(job_url)
    except Exception as exc:  # noqa: BLE001 - a failed fetch must not fail the run
        logger.warning(
            "deadline-queue: fetch failed for %s (%s); leaving unknown",
            job_url,
            exc,
        )
        return "unknown"
    if status in (404, 410):
        return "closed"
    if status != 200:
        return "unknown"
    visible = _visible_text(body).lower()
    if len(visible) < 50:
        # JS shell / empty page: nothing to judge from.
        return "unknown"
    if any(marker in visible for marker in _CLOSED_MARKERS):
        return "closed"
    return "live"


def _tracker_rejected(metadata: Any) -> bool:
    """True when the engine tracker (metadata.tracker) says rejected."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            return False
    if not isinstance(metadata, dict):
        return False
    tracker = metadata.get("tracker") or {}
    if not isinstance(tracker, dict):
        return False
    return tracker.get("stage") == "rejected" or tracker.get("outcome") == "rejected"


def determine_outcome(row: dict, liveness: str) -> Optional[str]:
    """Outcome for one queued row, or None when the check could not tell
    (fail-open: the row stays 'tracking' with its old outcome).
    Priority: rejected (tracker truth) > applied > closed > still_open."""
    if _tracker_rejected(row.get("metadata")):
        return OUTCOME_REJECTED
    if (row.get("apply_status") or "") in APPLIED_STATUSES:
        return OUTCOME_APPLIED
    if liveness == "closed":
        return OUTCOME_CLOSED
    if liveness == "live":
        return OUTCOME_STILL_OPEN
    return None


def enqueue_expired_jobs(conn) -> list[str]:
    """Moves newly expired IDLE jobs into the queue. Returns the enqueued
    job_ids. Idempotent: NOT EXISTS + ON CONFLICT means a re-run enqueues
    nothing twice."""
    with conn.cursor() as cur:
        cur.execute(ENQUEUE_SQL)
        rows = cur.fetchall() or []
    return [r["job_id"] if isinstance(r, dict) else r[0] for r in rows]


def get_current_job(conn) -> Optional[dict]:
    """The job GET /api/lockscreen/current would return right now, under
    the same ordering rule (imported from the dashboard, not copied —
    one source of truth)."""
    with conn.cursor() as cur:
        cur.execute(LOCKSCREEN_CURRENT_QUERY)
        row = cur.fetchone()
    return dict(row) if row else None


def check_queued_jobs(
    conn,
    fetcher: Optional[Callable[[str], tuple[int, str]]] = None,
) -> list[dict]:
    """Re-checks every tracking row. Returns the determinations made this
    run: [{job_id, title, company, outcome}] for rows whose outcome was
    determined. Unknown checks only stamp last_checked_at."""
    with conn.cursor() as cur:
        cur.execute(TRACKING_SQL)
        rows = [dict(r) for r in (cur.fetchall() or [])]
    determinations = []
    for row in rows:
        liveness = check_posting_live(row.get("job_url"), fetcher=fetcher)
        outcome = determine_outcome(row, liveness)
        with conn.cursor() as cur:
            if outcome is None:
                cur.execute(TOUCH_QUEUE_SQL, (row["job_id"],))
                continue
            state = (
                STATE_DONE
                if outcome in TERMINAL_OUTCOMES
                else STATE_TRACKING
            )
            cur.execute(UPDATE_QUEUE_SQL, (state, outcome, row["job_id"]))
        determinations.append(
            {
                "job_id": row["job_id"],
                "title": row.get("title"),
                "company": row.get("company"),
                "outcome": outcome,
            }
        )
    return determinations


def draft_advance_message(
    old_job: dict, reason: str, new_job: Optional[dict]
) -> str:
    """Drafts the advance note with Gemini through the platform LLM
    chain. Facts only — the prompt forbids inventing anything. Falls
    back to a deterministic template when the LLM is unavailable, so a
    verified advance is never lost to a quota error."""
    old_line = f"{old_job.get('title')} at {old_job.get('company')}"
    if new_job:
        new_tail = (
            "Your lock screen now shows "
            f"{new_job.get('title')} at {new_job.get('company')}."
        )
    else:
        new_tail = (
            "There is no next job in line yet — add one to keep the "
            "countdown going."
        )
    facts = f"Old dream job: {old_line}. Why it left: {reason}. {new_tail}"
    prompt = (
        "Write a short WhatsApp message (under 300 characters) telling "
        "Sri Omkar his dream-job lock screen just advanced to the next "
        "job. Use ONLY these verified facts, word them warmly and "
        f"plainly, invent nothing: {facts}"
    )
    try:
        return generate_text(prompt, temperature=0.2, task="deadline-advance").strip()[:4000]
    except Exception as exc:  # noqa: BLE001 - notification must never crash the run
        logger.warning("deadline-queue: LLM draft failed (%s); using template", exc)
        return f"Your dream-job lock screen advanced: {old_line} left because {reason}. {new_tail}"


def notify_advance(
    old_job: dict,
    reason: str,
    new_job: Optional[dict],
    *,
    draft_fn: Optional[Callable] = None,
    sender: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Sends the advance note on the existing WhatsApp alert path
    (notifier.send_whatsapp). Returns True when sent."""
    draft = draft_fn or draft_advance_message
    send = sender or send_whatsapp
    message = draft(old_job, reason, new_job)
    sent = send(message)
    if sent:
        logger.info(
            "deadline-queue: advance notification sent for %s",
            old_job.get("job_id"),
        )
    else:
        logger.warning(
            "deadline-queue: advance notification NOT sent "
            "(WhatsApp unavailable); alert logged instead"
        )
    return sent


def _run_daily_check(
    conn,
    fetcher: Optional[Callable[[str], tuple[int, str]]] = None,
    draft_fn: Optional[Callable] = None,
    sender: Optional[Callable[[str], bool]] = None,
) -> dict:
    """One daily run: snapshot the current job, enqueue newly expired
    jobs, re-check the queue, snapshot again. Notifies only when the
    current job changed because the old one was determined closed or
    rejected this run."""
    before = get_current_job(conn)
    enqueued = enqueue_expired_jobs(conn)
    determinations = check_queued_jobs(conn, fetcher=fetcher)
    after = get_current_job(conn)
    notified = False
    if before is not None and (
        after is None or after.get("job_id") != before.get("job_id")
    ):
        departed = next(
            (
                d
                for d in determinations
                if d["job_id"] == before["job_id"]
                and d["outcome"] in (OUTCOME_CLOSED, OUTCOME_REJECTED)
            ),
            None,
        )
        if departed is not None:
            notified = notify_advance(
                before,
                _REASON_TEXT[departed["outcome"]],
                after,
                draft_fn=draft_fn,
                sender=sender,
            )
    conn.commit()
    return {
        "enqueued": enqueued,
        "determinations": determinations,
        "previous_current": before.get("job_id") if before else None,
        "current": after.get("job_id") if after else None,
        "notified": notified,
    }


def run_daily_check(
    conn=None,
    fetcher: Optional[Callable[[str], tuple[int, str]]] = None,
    draft_fn: Optional[Callable] = None,
    sender: Optional[Callable[[str], bool]] = None,
) -> dict:
    """Runs one daily check. Opens its own connection when none is
    passed (the script path); tests pass a fake connection."""
    if conn is not None:
        return _run_daily_check(conn, fetcher, draft_fn, sender)
    with get_db_connection() as conn:
        return _run_daily_check(conn, fetcher, draft_fn, sender)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    summary = run_daily_check()
    print(json.dumps(summary, default=str))
    logger.info("deadline-queue: daily check done: %s", summary)


if __name__ == "__main__":
    main()
