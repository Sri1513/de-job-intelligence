-- migrations/010_deadline_queue.sql
-- Separate daily-tracking queue for the 7-day job-deadline system
-- (branch friday/lockscreen-deadline-queue).
--
-- Queue design DECIDED by the owner 2026-11-23: a SEPARATE LIST (this
-- table), not a new status in the existing tracker. A job enters the
-- queue once its derived deadline (posted_at + 7 days) has passed and
-- it is still apply_status IDLE; the daily worker re-checks each
-- queued job (posting still live? tracker rejected? applied?) and
-- records the outcome. The lockscreen's "current dream job" pointer
-- advances automatically — the /api/lockscreen/current ordering rule
-- only returns jobs whose deadline has not passed — and a verified
-- closed/rejected determination for the job that WAS current fires a
-- WhatsApp advance notification naming the old job, the reason, and
-- the next job in line.
--
-- State: 'tracking' (still watched daily) | 'done' (terminal outcome).
-- Outcome: NULL while undetermined; 'closed' (posting verified gone:
-- HTTP 404/410 or closed markers on a fetched page), 'rejected'
-- (tracker stage/outcome), 'applied' (apply_status SUBMITTED/MANUAL),
-- 'still_open' (posting verified live past its deadline — stays
-- 'tracking', checked again tomorrow).
--
-- Idempotent (IF NOT EXISTS = safe to run repeatedly).
--
-- Run on the VM with:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < migrations/010_deadline_queue.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)
-- CI never applies migrations; this is a manual-psql step only. Run it
-- BEFORE deploying the friday/lockscreen-deadline-queue branch — the
-- worker's INSERT references deadline_queue, so code-first would break
-- the daily check. Do NOT run it against any database you cannot rebuild.

CREATE TABLE IF NOT EXISTS scout.deadline_queue (
    job_id character varying(100) PRIMARY KEY
        REFERENCES scout.saved_jobs(job_id) ON DELETE CASCADE,
    deadline DATE NOT NULL,
    state TEXT NOT NULL DEFAULT 'tracking',
    first_queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    outcome TEXT
);

CREATE INDEX IF NOT EXISTS idx_deadline_queue_state
    ON scout.deadline_queue (state);
