-- Singular test: fails (returns rows) if int_job_skills ever holds
-- a duplicate (job_id, skill) pair. The model uses SELECT DISTINCT
-- and a composite unique_key, so this should always pass — it is
-- the tripwire that proves the grain holds.
select
    job_id,
    skill,
    count(*) as dup_count
from {{ ref('int_job_skills') }}
group by job_id, skill
having count(*) > 1
