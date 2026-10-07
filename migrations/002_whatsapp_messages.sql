-- migrations/002_whatsapp_messages.sql
-- Gives WhatsApp inbound messages their own table instead of stuffing
-- wa-<hash> stub rows into saved_jobs. Idempotent (IF NOT EXISTS / ON
-- CONFLICT = safe to run repeatedly).
--
-- After this, the WhatsApp pipeline writes raw messages to
-- scout.whatsapp_messages; the working job lead lives in
-- scout.outreach_tracking (unchanged); saved_jobs holds scraped jobs only.
--
-- Run on the VM with:
--   cd /opt/projects/de-job-intelligence
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < migrations/002_whatsapp_messages.sql
-- (DB_USER/DB_NAME come from your .env; defaults: postgres / job_scout_db)

CREATE TABLE IF NOT EXISTS scout.whatsapp_messages (
    message_id TEXT PRIMARY KEY,
    sender TEXT,
    body TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Backfill: copy raw messages out of the existing wa- saved_jobs stubs.
INSERT INTO scout.whatsapp_messages (message_id, body, received_at, status)
SELECT
    job_id,
    COALESCE(metadata->>'whatsapp_raw', ''),
    saved_at,
    'processed'
FROM scout.saved_jobs
WHERE job_id LIKE 'wa-%'
ON CONFLICT (message_id) DO NOTHING;

-- Remove the stubs from the main jobs list.
DELETE FROM scout.saved_jobs WHERE job_id LIKE 'wa-%';
