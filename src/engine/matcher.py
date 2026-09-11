# src/engine/matcher.py
import json
import re
from typing import Any

from src.core.config import settings


def load_skills_registry(category_slug: str = "data_engineering") -> dict:
    """Loads the skill taxonomy dictionary for match scoring."""
    slug = "devops" if "devops" in category_slug.lower() else "de"
    skills_file = settings.CONFIG_DIR / "skills" / f"skills_{slug}.json"

    if not skills_file.exists():
        raise FileNotFoundError(f"Skills taxonomy missing at {skills_file}")

    with open(skills_file, "r", encoding="utf-8") as f:
        return json.load(f)

def calculate_match_score(job_description: str, category_slug: str = "data_engineering") -> dict[str, Any]:
    """
    Computes an algorithmic fit score based on core tech stack coverage,
    supporting tools, and seniority alignment.
    """
    if not job_description:
        return {"score": 0, "matched_skills": [], "missing_skills": []}

    registry = load_skills_registry(category_slug)
    core_techs: list[str] = registry.get("core_techs", [])
    supporting_tools: list[str] = registry.get("supporting_tools", [])

    jd_lower = job_description.lower()

    matched_core = [tech for tech in core_techs if re.search(rf"\b{re.escape(tech)}\b", jd_lower)]
    matched_supporting = [tool for tool in supporting_tools if re.search(rf"\b{re.escape(tool)}\b", jd_lower)]

    # Scoring weights: 70% core tech presence, 30% supporting tool coverage
    core_score = (len(matched_core) / max(len(core_techs), 1)) * 70
    supporting_score = (len(matched_supporting) / max(min(len(supporting_tools), 10), 1)) * 30
    total_score = min(int(core_score + supporting_score), 100)

    return {
        "score": total_score,
        "matched_core": matched_core,
        "matched_supporting": matched_supporting,
        "missing_core": [t for t in core_techs if t not in matched_core]
    }
