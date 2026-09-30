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
        resume_path = RESUMES_DIR / "resume_devops.md"
    else:
        resume_path = RESUMES_DIR / "resume_de.md"

    if not resume_path.exists():
        raise FileNotFoundError(f"Base resume not found at: {resume_path}")

    with open(resume_path, "r", encoding="utf-8") as f:
        return f.read()


def clean_text(text: str) -> str:
    """Normalizes whitespace and strips non-printable characters."""
    if not text:
        return ""
    return " ".join(text.split())
