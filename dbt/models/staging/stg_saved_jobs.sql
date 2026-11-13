-- models/staging/stg_saved_jobs.sql
-- Staging for scout.saved_jobs: the first (and only) place raw job rows
-- get cleaned. Everything downstream — Silver, Gold marts, the
-- dashboard — reads this model, never the raw table.
--
-- WHAT IT CLEANS (and what it deliberately does NOT):
-- 1. job_url -> job_url_clean: strips email/marketing tracking params
--    (trkEmail, otpToken, lipi, utm_*) from the query string. These
--    params are how the SAME posting ends up with different URLs
--    (the Scenthound li-4488227236 case: a raw LinkedIn email tracking
--    URL landed verbatim in saved_jobs). Stripping them makes the URL
--    usable for dedup and display. The raw job_url is kept alongside
--    so nothing is lost — cleaning is additive, never destructive.
-- 2. company -> company_clean / company_key: trims and collapses
--    whitespace ('  Optum ' -> 'Optum'); company_key lowercases it for
--    grouping ('Optum' vs 'OPTUM' count once). Display keeps the
--    cleaned case; the key is for joins only.
-- 3. title: FLAGGED, not rewritten. If a title contains 'http' or is
--    over 150 chars it is almost certainly a mashed-in tracking URL
--    (Oct 4 diagnosis). Rewriting a title would invent data — the
--    parser fix upstream owns that. Staging just makes it visible:
--    title_contains_url, title_length.
-- 4. NO row filtering: DELETED rows stay in staging. Excluding them is
--    a mart decision, so audits can still see what was excluded.
-- 5. NO type changes: fit_score stays TEXT (the numeric-shape guard
--    lives in the marts, exactly like the hand-written views do it).
--
-- HOW THE URL CLEANING WORKS (plain Postgres, no extension):
-- split the URL at the first '?', split the query string on '&',
-- drop the banned params, re-join. The LEFT JOIN LATERAL keeps rows
-- with no query string at all (their kept-params list is NULL, and
-- the CASE falls back to the bare base URL). A URL whose every param
-- was banned also falls back to the base URL — never a trailing '?'.
with base as (
    select
        job_id,
        job_url,
        title,
        company,
        location,
        is_remote,
        salary_min,
        salary_max,
        fit_score,
        description,
        metadata,
        employment_type,
        sponsorship,
        job_category,
        ai_status,
        status,
        apply_status,
        applied_at,
        saved_at,
        split_part(job_url, '?', 1) as url_base,
        nullif(split_part(job_url, '?', 2), '') as query_string
    from {{ source('scout', 'saved_jobs') }}
),

kept_params as (
    select
        b.job_id,
        array_agg(p.param) as kept
    from base as b
    left join lateral
        unnest(string_to_array(b.query_string, '&')) as p(param)
        on true
    where p.param is not null
        and p.param not like 'trkEmail=%'
        and p.param not like 'otpToken=%'
        and p.param not like 'lipi=%'
        and p.param not like 'utm\_%='
    group by b.job_id
)

select
    b.job_id,
    b.job_url,
    -- The canonical URL: base + surviving params, or the bare base.
    case
        when coalesce(cardinality(k.kept), 0) = 0 then b.url_base
        else b.url_base || '?' || array_to_string(k.kept, '&')
    end as job_url_clean,
    -- True when at least one param was stripped: measures how much
    -- tracking junk the pipeline is still ingesting.
    (
        coalesce(cardinality(string_to_array(b.query_string, '&')), 0)
        > coalesce(cardinality(k.kept), 0)
    ) as had_tracking_params,
    b.title,
    (b.title ilike '%http%') as title_contains_url,
    length(b.title) as title_length,
    b.company,
    trim(regexp_replace(b.company, '\s+', ' ', 'g')) as company_clean,
    lower(trim(regexp_replace(b.company, '\s+', ' ', 'g'))) as company_key,
    b.location,
    b.is_remote,
    b.salary_min,
    b.salary_max,
    b.fit_score,
    b.description,
    b.metadata,
    b.employment_type,
    b.sponsorship,
    b.job_category,
    b.ai_status,
    b.status,
    b.apply_status,
    b.applied_at,
    b.saved_at
from base as b
left join kept_params as k
    on k.job_id = b.job_id
