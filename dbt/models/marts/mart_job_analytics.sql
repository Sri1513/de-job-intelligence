-- models/marts/mart_job_analytics.sql
-- Gold, Batch 6. Drop-in replacement for the hand-written view
-- scout.v_dashboard_job_analytics (migrations/004). The public MCP
-- surface reads it through src/protocols/dashboard_mcp.py
-- (_query_jobs): same columns, same order, same DELETED filter.
--
-- ONE deliberate value change, and it is the whole point of staging:
-- the view selects the RAW job_url; this mart selects
-- job_url_clean AS job_url. Same column name, cleaned value — the
-- dashboard keeps working unchanged and stops showing tracking junk
-- (214 of 2,330 URLs, verified 2026-11-12). Everything else is
-- byte-for-byte the view's logic, moved here from int_saved_jobs.

select
    job_id,
    title,
    company,
    location,
    job_category,
    fit_score,
    saved_at,
    job_url_clean as job_url,
    description_preview
from {{ ref('int_saved_jobs') }}
where status != 'DELETED'
