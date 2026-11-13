-- Singular test: fails (returns rows) if mart_job_skills ever
-- diverges from the hand-written view it replaces. Row-count check
-- plus (job_id, skill) set difference in both directions.
select
    'row_count' as check_name,
    (select count(*) from {{ ref('mart_job_skills') }})::text as mart_value,
    (select count(*) from scout.v_dashboard_job_skills)::text as view_value
where (select count(*) from {{ ref('mart_job_skills') }})
   <> (select count(*) from scout.v_dashboard_job_skills)

union all

select
    'missing_in_mart' as check_name,
    v.job_id || ':' || v.skill as mart_value,
    null::text as view_value
from scout.v_dashboard_job_skills v
where not exists (
    select 1 from {{ ref('mart_job_skills') }} m
    where m.job_id = v.job_id and m.skill = v.skill
)
limit 5

union all

select
    'extra_in_mart' as check_name,
    m.job_id || ':' || m.skill as mart_value,
    null::text as view_value
from {{ ref('mart_job_skills') }} m
where not exists (
    select 1 from scout.v_dashboard_job_skills v
    where v.job_id = m.job_id and v.skill = m.skill
)
limit 5
