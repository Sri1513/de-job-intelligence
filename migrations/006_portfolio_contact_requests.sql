-- migrations/006_portfolio_contact_requests.sql
-- Purpose: appointment / notify requests from the portfolio drawer.
-- When a visitor says they want an appointment or to be put in touch
-- and shares their name + contact info, the dashboard saves the request
-- and pings Omkar on WhatsApp (see src/dashboard/portfolio_contact.py).
--
-- HOW IT WORKS: one row per request. name/contact/reason/timings come
-- from a small model extraction call; raw_message always holds the
-- visitor's exact words, so a failed extraction never loses the details.
-- kind is 'appointment' or 'notify'; notified flips to TRUE once the
-- WhatsApp ping succeeds; status starts 'new' for Omkar to work.
--
-- HOW IT IS APPLIED: CI deploy does not run migrations. The dashboard
-- creates this table itself at startup via
-- ensure_contact_requests_table() in _dashboard_lifespan (same pattern
-- as the jobs pagination index, migrations/003). This file is the
-- record + the manual fallback:
--   docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" \
--     < migrations/006_portfolio_contact_requests.sql
--
-- Idempotent: CREATE TABLE IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS portfolio_contact_requests (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind TEXT NOT NULL DEFAULT 'appointment',
    name TEXT,
    contact_info TEXT,
    reason TEXT,
    timings TEXT,
    raw_message TEXT NOT NULL,
    notified BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'new'
);
