-- Singular test: fails (returns rows) if mart_job_analytics ever
-- diverges from the hand-written view it replaces. Row-count check
-- plus job_id set difference in both directions. A pass here is
-- what makes the mart a safe drop-in for the dashboard.
-- NOTE: each UNION branch is parenthesised — a bare LIMIT before a
-- UNION is a Postgres syntax error.
(select
    'row_count' as check_name,
    (select count(*) from {{ ref('mart_job_analytics') }})::text as mart_value,
    (select count(*) from scout.v_dashboard_job_analytics)::text as view_value
where (select count(*) from {{ ref('mart_job_analytics') }})
   <> (select count(*) from scout.v_dashboard_job_analytics))

union all

(select
    'missing_in_mart' as check_name,
    v.job_id as mart_value,
    null::text as view_value
from scout.v_dashboard_job_analytics v
where not exists (
    select 1 from {{ ref('mart_job_analytics') }} m
    where m.job_id = v.job_id
)
limit 5)

union all

(select
    'extra_in_mart' as check_name,
    m.job_id as mart_value,
    null::text as view_value
from {{ ref('mart_job_analytics') }} m
where not exists (
    select 1 from scout.v_dashboard_job_analytics v
    where v.job_id = m.job_id
)
limit 5)
