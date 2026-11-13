-- models/marts/mart_market_metrics.sql
-- Gold, Batch 6. Drop-in replacement for the hand-written view
-- scout.v_dashboard_market_metrics (migrations/004). The public MCP
-- surface reads it through src/protocols/dashboard_mcp.py
-- (_fetch_overview): same columns, same order, same DELETED filter,
-- same GROUP BY job_category.
--
-- The numeric guard lives in int_saved_jobs now (fit_score_num is
-- NULL for non-numeric text), so avg() and the >= 80 filter behave
-- exactly like the view's inline CASE — NULLs are ignored by both.
-- unique_hiring_companies keeps the view's count(DISTINCT company)
-- on the RAW company name: staging's company_clean exists and would
-- merge case variants ('Optum' vs 'OPTUM'), but that changes a
-- number the dashboard shows today, so it stays a one-line change
-- for a later batch if he wants it, not a silent change in this one.

select
    job_category,
    count(*) as total_jobs_tracked,
    round(avg(fit_score_num), 1) as average_fit_score,
    count(*) filter (where fit_score_num >= 80) as high_match_jobs,
    count(distinct company) as unique_hiring_companies,
    max(saved_at) as last_ingested_at
from {{ ref('int_saved_jobs') }}
where status != 'DELETED'
group by job_category
