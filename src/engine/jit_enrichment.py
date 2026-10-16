# src/engine/jit_enrichment.py
"""Just-in-time description enrichment for the tailoring path.
The email pipeline stores a one-line placeholder ("Extracted via email
alert...") when widening misses. If that job is later tailored, the
placeholder poisons both the healthcare slot-routing check and the
deterministic bullet ranking, which read the description as the JD.

This module fixes that at read time: when the tailoring prompt builder
loads a job whose description is still a placeholder, it attempts ONE
live widening pass (the same board chain the email pipeline uses) and
writes the real description back to saved_jobs.

Scope and honesty rules:
- Only the tailoring path calls this. Dashboard reads never trigger a
  live scrape — a phone tap must not hang on network I/O.
- Fail-open: ANY failure (no match, 429, exception) leaves the job
  unchanged and tailoring proceeds with the placeholder.
- Attempt guard: a failed attempt is recorded in
  metadata["jit_enrichment"] = {"last_attempt": iso, "attempts": n} and
  suppresses retries for ENRICHMENT_RETRY_DAYS. A permanently-missing JD
  must not re-scrape on every view. Success needs no guard — the
  placeholder check short-circuits once the description is real.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from src.engine.matcher import calculate_local_fit_score
from src.engine.pipeline_utils import (
    _is_placeholder_description,
    _is_real_description,
    extract_linkedin_id,
    get_cached_resume,
)
from src.engine.scraper import (
    fetch_linkedin_guest_job,
    fetch_scraped_jobs,
    linkedin_throttle_sleep,
)

logger = logging.getLogger(__name__)

ENRICHMENT_RETRY_DAYS = 7
WIDENING_BOARDS = ("linkedin", "indeed", "zip_recruiter")

_US_STATE_ABBR = frozenset(
    s.lower()
    for s in (
        "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
        "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
    ).split()
)
_US_STATE_NAMES = frozenset(
    (
        "alabama alaska arizona arkansas california colorado connecticut delaware "
        "florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana "
        "maine maryland massachusetts michigan minnesota mississippi missouri montana "
        "nebraska nevada hampshire jersey mexico york carolina dakota ohio oklahoma "
        "oregon pennsylvania rhode island carolina dakota tennessee texas utah vermont "
        "virginia washington wisconsin wyoming district of columbia"
    ).split()
)
_US_MARKERS = ("united states", "usa", "u.s.a", "u.s.")
_NON_US_MARKERS = (
    "united kingdom",
    "canada",
    "india",
    "australia",
    "ireland",
    "germany",
    "france",
    "netherlands",
    "spain",
    "mexico",
    "brazil",
    "singapore",
    "philippines",
    "pakistan",
)
_REMOTE_US_LABELS = frozenset(
    {"remote", "remote us", "remote usa", "remote united states", "us remote", "usa remote"}
)


def is_us_location(job: dict) -> bool:
    """
    Best-effort check that a JobSpy result is a US posting.

    JobSpy's location filter is fuzzy, and the same role is often reposted from
    several offices — when duplicates match, this picks the US posting. Explicit
    non-US markers reject first (guards ambiguous abbreviations like IN), then
    positive US evidence (state names/abbreviations, USA markers, US remote
    labels) accepts. Remote results with no parsable location are accepted
    because the JobSpy query itself is US-scoped.
    """
    loc = (job.get("location") or "").strip()
    low = loc.lower()

    if any(m in low for m in _NON_US_MARKERS):
        return False
    if any(m in low for m in _US_MARKERS):
        return True
    if low in _REMOTE_US_LABELS:
        return True
    tokens = set(re.split(r"[^a-z0-9]+", low))
    if tokens & _US_STATE_ABBR:
        return True
    if any(name in low for name in _US_STATE_NAMES):
        return True
    if not loc and job.get("is_remote"):
        return True
    return False


def _find_qualified_match(found_jobs: list, company: str, linkedin_id: str | None = None):
    """First JobSpy result matching the job's company + a relevant role.

    Exact match wins: a JobSpy result whose URL carries the same LinkedIn
    job ID IS the posting — accept it outright, no fuzzy company/title
    checks. Falls back to the fuzzy company + role + US-location match.
    """
    for fj in found_jobs:
        fj_url = str(fj.get("job_url") or "")
        if linkedin_id and f"/jobs/view/{linkedin_id}" in fj_url:
            logger.info(
                "   -> Exact LinkedIn ID match: '%s' at '%s'",
                fj.get("title"), fj.get("company"),
            )
            return fj

    target_company_lower = (company or "").lower()
    for fj in found_jobs:
        fj_company = str(fj.get("company") or "").lower()
        fj_title = str(fj.get("title") or "").lower()

        is_company_match = (target_company_lower in fj_company) or (
            fj_company in target_company_lower
        )
        is_sponsored = any(bad in fj_title for bad in ["promoted", "sponsored", "ad"])
        is_relevant_role = any(
            kw in fj_title for kw in ["data", "etl", "engineer", "developer", "analytics"]
        )
        # The same role is often reposted from multiple offices; only accept
        # the US posting so the pipeline never points at a foreign listing.
        if (
            is_company_match
            and is_relevant_role
            and not is_sponsored
            and is_us_location(fj)
        ):
            logger.info(
                "   -> Qualified match found: '%s' at '%s'",
                fj.get("title"), fj.get("company"),
            )
            return fj
    return None


def enrich_job_description(title: str, company: str, job_url: str | None) -> dict | None:
    """One live widening pass for a single job; qualified job dict or None.

    Walks LinkedIn -> Indeed -> ZipRecruiter (each with its own rate limit),
    then one targeted LinkedIn guest-API request for the exact posting ID as
    a last resort. Returns None on any miss or failure — callers treat None
    as "keep the placeholder". Never raises.
    """
    try:
        if not company or company == "Unknown":
            return None
        search_query = f"{title} {company}"
        linkedin_id = extract_linkedin_id(job_url)

        for idx, board in enumerate(WIDENING_BOARDS):
            if idx > 0:
                # Don't fire three JobSpy bursts back-to-back; LinkedIn 429s
                # the burst pattern even from a residential IP.
                linkedin_throttle_sleep("LINKEDIN_WIDEN_DELAY_S", 4.0)
            found_jobs = fetch_scraped_jobs(
                search_term=search_query,
                location="United States",
                results_wanted=15,
                hours_old=168,
                is_remote=True,
                site_name=[board],
            )
            qualified = _find_qualified_match(found_jobs, company, linkedin_id)
            if qualified:
                return qualified
            logger.info("   -> %s widening missed; trying next board...", board)

        if linkedin_id:
            logger.info("   -> All board widening missed; trying LinkedIn guest API...")
            guest_job = fetch_linkedin_guest_job(linkedin_id)
            if guest_job and _is_real_description(guest_job.get("description")):
                guest_job["title"] = guest_job.get("title") or title
                guest_job["company"] = guest_job.get("company") or company
                guest_job["job_url"] = guest_job.get("job_url") or job_url
                return guest_job
        return None
    except Exception as exc:
        logger.warning("JIT widening pass failed for '%s': %s", title, exc)
        return None


def _load_metadata(job: dict) -> dict:
    meta = job.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _guard_allows(meta: dict) -> bool:
    """True when no recent failed enrichment attempt suppresses a retry."""
    guard = meta.get("jit_enrichment") or {}
    last = guard.get("last_attempt")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except (ValueError, TypeError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_dt >= timedelta(days=ENRICHMENT_RETRY_DAYS)


def maybe_enrich_job_for_tailoring(job: dict, conn=None) -> dict:
    """Replace a placeholder description with the real one, just in time.

    job: the loaded job dict (DB row or WhatsApp override shape).
    conn: open DB connection for write-back, or None when the job has no
    DB row (override path) — enrichment then applies to the in-memory
    dict only.

    Fail-open: never raises; on any failure the job is returned unchanged
    and tailoring proceeds with the placeholder.
    """
    try:
        if not _is_placeholder_description(job.get("description")):
            return job

        meta = _load_metadata(job)
        if not _guard_allows(meta):
            logger.info(
                "JIT enrichment skipped for '%s': recent attempt on record.",
                job.get("title"),
            )
            return job

        enriched = enrich_job_description(
            job.get("title") or "Data Engineer",
            job.get("company") or "Unknown",
            job.get("job_url"),
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        guard = meta.get("jit_enrichment") or {}
        guard["last_attempt"] = now_iso
        guard["attempts"] = int(guard.get("attempts") or 0) + 1
        meta["jit_enrichment"] = guard
        job["metadata"] = meta

        if enriched and _is_real_description(enriched.get("description")):
            real_desc = enriched["description"]
            job["description"] = real_desc
            if enriched.get("job_url"):
                job["job_url"] = enriched["job_url"]

        if conn is not None and job.get("job_id"):
            if _is_real_description(job.get("description")) and enriched:
                # Heal the row: real description + locally recomputed fit
                # (0 API cost — the old score was computed on the placeholder).
                resume_text = get_cached_resume(job.get("job_category"))
                healed_eval = calculate_local_fit_score(
                    resume_text=resume_text,
                    job_description=job["description"],
                    job_title=job.get("title"),
                    job_category=job.get("job_category"),
                )
                meta["matched_skills"] = healed_eval.get("matched_skills", [])
                meta["missing_skills"] = healed_eval.get("missing_skills", [])
                meta["semantic_match"] = healed_eval.get("semantic_match", 0)
                meta["skill_match"] = healed_eval.get("skill_match", 0)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE saved_jobs
                        SET description = %s, fit_score = %s, metadata = %s
                        WHERE job_id = %s;
                        """,
                        (
                            job["description"],
                            str(healed_eval.get("score", 0)),
                            json.dumps(meta),
                            job["job_id"],
                        ),
                    )
                logger.info(
                    "JIT enrichment healed description for job '%s'.", job.get("job_id")
                )
            else:
                # Miss or failure: persist only the attempt guard so the
                # next tailoring run doesn't re-scrape.
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE saved_jobs SET metadata = %s WHERE job_id = %s;",
                        (json.dumps(meta), job["job_id"]),
                    )
        return job
    except Exception as exc:
        logger.warning(
            "JIT enrichment failed for job '%s': %s", job.get("job_id"), exc
        )
        return job
