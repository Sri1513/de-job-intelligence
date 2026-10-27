# src/core/utils.py
from functools import lru_cache

from src.core.config import settings

RESUMES_DIR = settings.CONFIG_DIR / "resumes"


@lru_cache(maxsize=4)
def get_cached_resume(category_slug: str) -> str:
    """
    Loads and caches the base Markdown resume for a given category slug.
    Prevents repeated disk I/O during high-throughput batch runs.
    """
    slug = category_slug.lower()
    if "devops" in slug:
        candidates = [RESUMES_DIR / "resume_devops.md"]
    else:
        # Data-engineering roles prefer the wide resume (resume_wide.md,
        # extracted from Sri_Omkar-2.pdf); resume_de.md stays as fallback.
        candidates = [RESUMES_DIR / "resume_wide.md", RESUMES_DIR / "resume_de.md"]

    for resume_path in candidates:
        if resume_path.exists():
            with open(resume_path, "r", encoding="utf-8") as f:
                return f.read()

    raise FileNotFoundError(f"Base resume not found at: {candidates[0]}")


def clean_text(text: str) -> str:
    """Normalizes whitespace and strips non-printable characters."""
    if not text:
        return ""
    return " ".join(text.split())
