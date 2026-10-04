# Migration Notes — `friday/fix-session-portability`

Branch: `friday/fix-session-portability`
Date: 2026-10-02

## 1. New DB columns on `saved_jobs`

Run once (uses the `scout` schema like the existing
`scripts/migrate_apply_columns.py`):

```sql
ALTER TABLE scout.saved_jobs
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claimed_by TEXT,
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS failure_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_saved_jobs_apply_status_retry
    ON scout.saved_jobs (apply_status, next_retry_at);
```

| Column | Purpose |
|---|---|
| `claimed_at` / `claimed_by` | Atomic worker claim lease; stale `IN_PROGRESS` claims (>30 min) are reclaimed automatically |
| `attempts` | Run count; FAILED jobs are retried with backoff up to 3 attempts, then parked |
| `next_retry_at` | Backoff timestamp (`5 min × attempts`); the claim query skips jobs until it elapses |
| `failure_reason` | Taxonomy for the dashboard: `login_wall`, `captcha`, `session_expired`, `field_unmapped`, `upload_failed`, `rate_limited`, `agent_error`, `unknown` |

**Graceful degradation:** the worker runs fine without these columns (it logs a
warning and falls back to a simpler atomic claim); apply the SQL to enable
leases, retries, and the failure taxonomy.

## 2. `status` vs `apply_status` unification

- `status` (legacy display column, read by dashboard UI) and `apply_status`
  (worker state machine) could disagree. `/api/jobs/update-status`
  ("Mark Applied") now writes **both**: `applied` → `status='applied'` +
  `apply_status='SUBMITTED'` + `applied_at=NOW()`.
- `/api/applications/queue` now refuses (`409`) to re-queue `SUBMITTED` jobs
  and is idempotent for already-`QUEUED`/`IN_PROGRESS` jobs.
- `applied_at` is now set **only** on `SUBMITTED` (previously also on
  `PENDING_REVIEW`).

## 3. Session model change (the actual portability fix)

- `data/browser_profiles/<platform>/` (persistent Playwright profile) is now
  the session source of truth; `config/auth_states/*.json` are **first-boot
  seed only** — injected once, and only when the profile shows logged-out.
- The old `load_combined_storage_state()` (merge-and-reinject every run) is
  gone; legacy `_active_session.json` is deleted on first run.
- New statuses: `NEEDS_HUMAN` (session dead, no usable seed — check WhatsApp
  alerts / dashboard instead of a silent FAILED).
- On a new machine (e.g. the Oracle VM), create the session **there**:
  `python scripts/manual_login.py linkedin` (headed; use `xvfb-run -a` on a
  headless server). Do NOT copy cookie files across machines — LinkedIn binds
  sessions to IP/user-agent and the transplant will be checkpointed.
- Indeed runs behind a Cloudflare managed challenge: on servers it needs
  headed mode under Xvfb (`HEADLESS=false` + `xvfb-run`), headless will fail
  regardless of cookies.

## 4. New environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini`, `muse` (Meta Model API), or `auto` (failover: Groq free → Gemini free → Gemini paid) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model for `LLM_PROVIDER=gemini` and the free step of `auto` |
| `MODEL_API_KEY` | — | Required when `LLM_PROVIDER=muse` |
| `META_MODEL_API_BASE_URL` | `https://api.meta.ai/v1` | Meta Model API endpoint |
| `MUSE_SPARK_MODEL` | `muse-spark-1.3` | Model id for `LLM_PROVIDER=muse` |
| `GROQ_API_KEY` | — | Groq key (free tier, no card). First in `auto` chain and analysis pipeline |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model id (e.g. `llama-3.3-70b-versatile`) |
| `GEMINI_PAID_API_KEY` | — | Paid-tier Gemini key. Last resort of the `auto` apply chain ONLY; analysis never uses it. Also accepts `GEMINI_PAID_API_KEYS` (comma-separated) / `GEMINI_PAID_API_KEY_2..9` |
| `GEMINI_PAID_MODEL` | `gemini-2.5-flash` | Model for the paid step of `auto` |
| `APPLY_ALLOW_PAID` | `true` | Set `false` to keep the `auto` chain free-only (paid step excluded) |
| `POLL_INTERVAL` / `BATCH_LIMIT` | `30` / `5` | Apply worker daemon tuning (unchanged) |

### LLM failover policy (`LLM_PROVIDER=auto`)

- **Analysis** (fit score, sponsorship, skill extraction): Groq free → Gemini free pool
  (`GEMINI_API_KEY[_N]`). Paid keys are never used. Every evaluation logs which
  provider served it; Groq quota exhaustion is skipped for the rest of the day.
- **Apply** (browser agent): Groq free → Gemini free → Gemini paid
  (`GEMINI_PAID_API_KEY[_N]`). Paid Gemini engages only after free quotas are
  exhausted, and the engagement is logged loudly. Quota errors park a free
  provider until ~midnight UTC; billing errors park for 30 min so a mid-day
  top-up recovers.
- All failovers, exhaustion markings, and paid engagements are logged and
  visible via `job-admin logs --service apply-worker`.

## 5. Internal API changes

- `src.workers.apply_worker`: `get_queued_apply_jobs()` /
  `update_apply_status()` replaced by `claim_queued_jobs()` /
  `finalize_job()` / `classify_failure_reason()`. `process_apply_queue()` and
  `run_worker_daemon()` signatures are unchanged.
- `src.engine.browser_agent`: `load_combined_storage_state()` replaced by
  `load_seed_cookies()` + `ensure_session()` + `check_login_state()` +
  `build_browser()` + `get_llm()`. `autofill_job_application()` signature is
  unchanged. New status constant `STATUS_NEEDS_HUMAN`.
- `scripts/manual_login.py`: new one-time login helper (see `--help`).
- `scripts/export_session.py`: unchanged; still useful for producing seed
  JSONs, but seeds are now fallback-only.
