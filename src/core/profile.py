# src/core/profile.py
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.core.config import settings

logger = logging.getLogger("de-job-intelligence.profile")


class WorkAuthorization(BaseModel):
    authorized_in_us: bool = True
    requires_sponsorship_now_or_future: bool = False


class ApplicantProfile(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str
    location: str
    linkedin: str
    website: str | None = None
    github: str | None = None
    resume_path: str | None = "config/resumes/default_resume.pdf"
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    custom_answers: dict[str, Any] = Field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def get_resolved_resume_path(self, base_dir: Path) -> Path | None:
        """Resolves resume_path to an absolute file path on disk."""
        if not self.resume_path:
            return None
        candidate = Path(self.resume_path)
        if candidate.is_absolute():
            return candidate if candidate.exists() else None
        resolved = (base_dir / candidate).resolve()
        return resolved if resolved.exists() else None


def load_applicant_profile(profile_path: Path | str | None = None) -> ApplicantProfile:
    """Loads and validates an applicant profile from JSON.

    Defaults to settings.active_profile_path.
    """
    path = Path(profile_path) if profile_path else settings.active_profile_path

    if not path.exists():
        logger.warning("Applicant profile not found at %s. Using fallback defaults.", path)
        return ApplicantProfile(
            first_name="Default",
            last_name="Applicant",
            email="applicant@example.com",
            phone="+1-000-000-0000",
            location="Remote, US",
            linkedin="https://linkedin.com",
        )

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return ApplicantProfile.model_validate(data)


# Active default profile instance
applicant_profile = load_applicant_profile()
