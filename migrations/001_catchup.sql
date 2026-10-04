-- migrations/001_catchup.sql
-- Consolidated schema catch-up for the friday/fix-session-portability branch.
--
-- The branch's code references columns that were never created on databases
-- built by earlier versions (e.g. get_job_by_id selects ai_notes; the apply
-- worker expects the lease/retry columns). This file adds every column the
-- branch touches, idempotently (IF NOT EXISTS = safe to run repeatedly).
--
-- Run on the VM with:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < migrations/001_catchup.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)

ALTER TABLE scout.saved_jobs
    ADD COLUMN IF NOT EXISTS ai_notes TEXT,
    ADD COLUMN IF NOT EXISTS ai_error TEXT,
    ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS apply_notes TEXT,
    ADD COLUMN IF NOT EXISTS apply_status TEXT,
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claimed_by TEXT,
    ADD COLUMN IF NOT EXISTS failure_reason TEXT,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_screenshot_path TEXT;

CREATE INDEX IF NOT EXISTS idx_saved_jobs_apply_status_retry
    ON scout.saved_jobs (apply_status, next_retry_at);
