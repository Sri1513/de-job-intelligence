# src/workers/local_llm_backfill.py
"""Manual trigger for the local-LLM (qwen2.5:3b) fallback queue.

Drains AI-pending/FAILED saved_jobs rows through the local 3B, serially,
one job at a time. The queue only processes rows the free Gemini -> Groq
chain already failed, so the local tier only ever runs after both free
tiers are exhausted for a given row.

Usage:
    python -m src.workers.local_llm_backfill --limit 5
    python -m src.workers.local_llm_backfill --limit 2 --dry-run
    python -m src.workers.local_llm_backfill --job-ids li-4480161700,li-4479259320

No scheduler invokes this yet (manual CLI first; scheduling is a
follow-up decision). Never runs inline during ingestion.
"""
import argparse
import json
import logging
import sys

from src.engine.local_llm_queue import run_local_fallback_batch

logger = logging.getLogger("de-job-intelligence.local-llm-backfill")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Drain AI-pending/FAILED jobs through the local 3B fallback queue."
    )
    parser.add_argument("--limit", type=int, default=5,
                        help="max rows to drain (default 5)")
    parser.add_argument("--job-ids", default="",
                        help="comma-separated job_ids to drain instead of the pending query")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the three local-LLM tasks but write nothing")
    args = parser.parse_args(argv)

    job_ids = [j.strip() for j in args.job_ids.split(",") if j.strip()] or None
    summary = run_local_fallback_batch(
        limit=args.limit, job_ids=job_ids, dry_run=args.dry_run
    )
    print(f"Local-LLM fallback batch result: {json.dumps(summary, indent=2, default=str)}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
