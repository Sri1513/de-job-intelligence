# src/engine/matcher.py
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from src.core.config import settings

logger = logging.getLogger(__name__)

_SKILLS_CACHE: dict[str, dict] = {}
_RESUME_CACHE: dict[str, str] = {}

SOFT_SKILLS_BLACKLIST = {
    "communication",
    "teamwork",
    "leadership",
    "problem solving",
    "problem-solving",
    "analytical skills",
    "interpersonal skills",
    "agile",
    "scrum",
    "fast-paced",
    "detail-oriented",
    "self-starter",
}


def _resolve_slug(category: str) -> str:
    return "devops" if "devops" in (category or "").lower() else "de"


def get_cached_resume(job_category: str = "data_engineering") -> str:
    """Safely retrieves and caches the user's resume content using settings paths."""
    global _RESUME_CACHE
    slug = _resolve_slug(job_category)

    if slug not in _RESUME_CACHE:
        filename = f"resume_{slug}.md"
        candidate_paths = []

        # Check standard config and base directories from settings
        if hasattr(settings, "RESOURCES_DIR"):
            candidate_paths.append(settings.RESOURCES_DIR / filename)
        if hasattr(settings, "CONFIG_DIR"):
            candidate_paths.append(settings.CONFIG_DIR / "resources" / filename)
            candidate_paths.append(settings.CONFIG_DIR / filename)
        if hasattr(settings, "BASE_DIR"):
            candidate_paths.append(settings.BASE_DIR / "resources" / filename)

        loaded_text = None
        for path in candidate_paths:
            if path.is_file():
                try:
                    loaded_text = path.read_text(encoding="utf-8")
                    break
                except Exception as e:
                    logger.warning(f"Could not read resume from {path}: {e}")

        if loaded_text:
            _RESUME_CACHE[slug] = loaded_text
        else:
            _RESUME_CACHE[slug] = (
                "Senior Data Engineer with 8+ years of experience specializing in Apache Spark, "
                "PySpark, Python, SQL, AWS, Azure, Snowflake, Databricks, and Lakehouse architectures."
            )

    return _RESUME_CACHE[slug]


def load_skills_registry(job_category: str = "data_engineering") -> dict:
    """Loads and caches skill taxonomy dictionary using settings paths."""
    global _SKILLS_CACHE
    slug = _resolve_slug(job_category)

    if slug not in _SKILLS_CACHE:
        filename = f"skills_{slug}.json"
        candidate_paths = []

        if hasattr(settings, "CONFIG_DIR"):
            candidate_paths.append(settings.CONFIG_DIR / "skills" / filename)
            candidate_paths.append(settings.CONFIG_DIR / filename)
        if hasattr(settings, "RESOURCES_DIR"):
            candidate_paths.append(settings.RESOURCES_DIR / filename)
        if hasattr(settings, "BASE_DIR"):
            candidate_paths.append(settings.BASE_DIR / "resources" / filename)

        loaded_json = None
        for path in candidate_paths:
            if path.is_file():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        loaded_json = json.load(f)
                        break
                except Exception as e:
                    logger.warning(f"Could not read taxonomy from {path}: {e}")

        if loaded_json:
            _SKILLS_CACHE[slug] = loaded_json
        else:
            _SKILLS_CACHE[slug] = {
                "seniority_keywords": ["senior", "lead", "principal", "staff"],
                "role_keywords": ["engineer", "developer", "architect"],
                "core_techs": ["python", "sql", "spark", "pyspark", "databricks"],
                "supporting_tools": ["aws", "azure", "docker", "airflow", "snowflake", "dbt"],
                "experience_keywords": ["5+", "6+", "7+", "8+"],
            }

    return _SKILLS_CACHE[slug]


# Alias for legacy compatibility
load_skill_config = load_skills_registry


def calculate_local_fit_score(
    resume_text: str = "",
    job_description: str = "",
    job_title: str = "",
    job_category: str = "data_engineering",
    ai_extracted_skills: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Computes a hybrid fit score:
    - 60% Rule-based (Seniority + Demanded Skill Coverage + Experience Keywords)
    - 40% TF-IDF Cosine Similarity
    Requires 0 Gemini API tokens.
    """
    if not resume_text:
        resume_text = get_cached_resume(job_category)

    config = load_skills_registry(job_category)
    seniority_terms: list = config.get("seniority_keywords", [])
    role_terms: list = config.get("role_keywords", [])
    core_techs: list = config.get("core_techs", [])
    supporting_tools: list = config.get("supporting_tools", [])
    exp_terms: list = config.get("experience_keywords", [])

    all_tracked_skills = [s.lower() for s in (core_techs + supporting_tools)]

    if not job_description and not ai_extracted_skills:
        return {
            "score": 0,
            "semantic_match": 0,
            "skill_match": 0,
            "matched_skills": [],
            "missing_skills": all_tracked_skills,
            "matched_core": [],
            "matched_supporting": [],
            "missing_core": core_techs,
        }

    job_lower = (job_description or "").lower()
    title_lower = (job_title or "").lower()
    resume_lower = (resume_text or "").lower()

    # 1. Seniority alignment
    if any(term in title_lower for term in seniority_terms):
        seniority_score = 25
    elif any(term in title_lower for term in role_terms):
        seniority_score = 20
    else:
        seniority_score = 10

    # 2. Skill gap extraction (strip soft-skills noise)
    clean_ai_skills = [
        s.lower().strip()
        for s in (ai_extracted_skills or [])
        if s.lower().strip() not in SOFT_SKILLS_BLACKLIST
    ]

    regex_matched_tracked = [
        skill
        for skill in all_tracked_skills
        if re.search(rf"\b{re.escape(skill)}\b", job_lower, re.IGNORECASE)
    ]

    job_demanded_skills = list(set(clean_ai_skills + regex_matched_tracked))
    if not job_demanded_skills:
        job_demanded_skills = all_tracked_skills[:5]

    matched_skills = [
        skill
        for skill in job_demanded_skills
        if re.search(rf"\b{re.escape(skill)}\b", resume_lower, re.IGNORECASE)
    ]

    missing_skills = [skill for skill in job_demanded_skills if skill not in matched_skills]

    skill_coverage = len(matched_skills) / len(job_demanded_skills) if job_demanded_skills else 0.6
    tech_score = skill_coverage * 65

    # 3. Experience level alignment
    exp_score = 10 if any(exp in job_lower for exp in exp_terms) else 5
    rule_based_score = (25 if seniority_score == 25 else 15) + tech_score + exp_score

    # 4. Semantic TF-IDF Cosine Similarity
    vectorizer = TfidfVectorizer(stop_words="english")
    try:
        tfidf_matrix = (
            vectorizer.fit_transform([resume_text, job_description])
            if job_description
            else vectorizer.fit_transform([resume_text, resume_text])
        )
        similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0]
        semantic_score = float(similarity) * 100
    except Exception:
        semantic_score = 0.0

    final_score = int(round((rule_based_score * 0.6) + (semantic_score * 0.4)))
    final_score = max(0, min(100, final_score))

    return {
        "score": final_score,
        "semantic_match": int(round(semantic_score)),
        "skill_match": int(round(rule_based_score)),
        "matched_skills": matched_skills,
        "missing_skills": missing_skills,
        "matched_core": [t for t in core_techs if t.lower() in matched_skills],
        "matched_supporting": [s for s in supporting_tools if s.lower() in matched_skills],
        "missing_core": [t for t in core_techs if t.lower() not in matched_skills],
    }


def calculate_match_score(
    job_description: str,
    category_slug: str = "data_engineering",
    job_title: str = "",
    resume_text: str = "",
) -> Dict[str, Any]:
    """Compatibility wrapper exposing the newer signature while utilizing the full engine."""
    return calculate_local_fit_score(
        resume_text=resume_text,
        job_description=job_description,
        job_title=job_title,
        job_category=category_slug,
    )
