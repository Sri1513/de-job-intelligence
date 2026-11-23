-- migrations/011_lockscreen_pin.sql
-- One pinned lock-screen job (branch friday/lockscreen-pin).
--
-- Owner decision 2026-11-23: BOTH ways stay — he tells Gemini a job
-- and it updates his lock screen (a pin), AND the automatic advance
-- keeps working (when the pinned/current job closes, the next one takes
-- its place). One endpoint, two inputs: the pin wins while it is live,
-- the auto pick takes over when the pin is gone.
--
-- The table holds exactly ONE row (id is always 1): one job on the
-- lock screen at a time. pin_lockscreen_job (MCP tool, called by
-- Gemini) upserts it; clear_lockscreen_pin deletes it; the daily
-- deadline-queue worker deletes it when the pinned job is verified
-- closed/rejected/applied, so GET /api/lockscreen/current falls back
-- to the auto pick. A pin is only honoured while the job still
-- qualifies (IDLE, posted date known, deadline not passed, not
-- verified gone) — a stale pin never 404s the endpoint while an auto
-- job exists. Deleting the saved job deletes the pin (CASCADE).
--
-- Idempotent (IF NOT EXISTS = safe to run repeatedly). No backfill:
-- nothing was pinned before this table existed.
--
-- Run on the VM with:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < migrations/011_lockscreen_pin.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)
-- CI never applies migrations; this is a manual-psql step only. Run it
-- BEFORE deploying the friday/lockscreen-pin branch — the MCP tools,
-- the endpoint, and the worker all reference lockscreen_pin, so
-- code-first would break them. Do NOT run it against any database you
-- cannot rebuild.

CREATE TABLE IF NOT EXISTS scout.lockscreen_pin (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    job_id character varying(100) NOT NULL
        REFERENCES scout.saved_jobs(job_id) ON DELETE CASCADE,
    pinned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pinned_via TEXT
);
