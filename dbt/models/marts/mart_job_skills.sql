-- models/marts/mart_job_skills.sql
-- Gold, Batch 6. Drop-in replacement for the hand-written view
-- scout.v_dashboard_job_skills (migrations/005). The public MCP
-- surface reads it through src/protocols/dashboard_mcp.py
-- (_fetch_top_skills, _fetch_skill_count, and the skill JOIN in
-- _query_jobs): same columns (job_id, skill), same DISTINCT, same
-- DELETED filter. The JSON expansion itself moved to
-- int_job_skills, so this mart is a thin filter — the view used to
-- re-run the expansion on every single read.

select distinct
    job_id,
    skill
from {{ ref('int_job_skills') }}
where status != 'DELETED'
