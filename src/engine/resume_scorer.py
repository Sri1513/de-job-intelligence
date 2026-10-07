"""Phase 4: deterministic post-generation resume scorer.

Answers "did this tailored resume actually land the JD?" without an LLM:
  - exact word-boundary + taxonomy-alias + rapidfuzz(>=80) matching of
    every weighted JD skill against the generated resume text
  - category weights (tech 1.5x, governance 1.25x, else 1.0x)
  - evidence bonus: skill proven in a bullet scores more than listed once
  - stuffing penalty: -3 per skill appearing more than 4 times
    (keyword-stuffed resumes fail human review)

Score = 100 * matched_weight / total_weight - penalties, floored at 0.
"""

import logging
import re
from typing import Dict, List

from src.engine.skill_normalizer import _surface_index, load_taxonomy

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 80
STUFFING_LIMIT = 4
STUFFING_PENALTY = 3

CATEGORY_WEIGHTS = {
    "etl_tools": 1.5,
    "bigdata": 1.5,
    "cloud": 1.5,
    "languages": 1.5,
    "databases": 1.5,
    "devops": 1.25,
    "governance": 1.25,
    "bi": 1.0,
    "concepts": 1.0,
    "process": 0.8,
    "domain": 0.8,
}


def _category_of(canonical_id: str) -> str:
    for skill in load_taxonomy().get("skills", []):
        if skill["id"] == canonical_id:
            return skill["category"]
    return "concepts"


def _resume_text(payload: Dict) -> Dict[str, str]:
    """Split the tailored payload into scannable sections."""
    bullets = payload.get("experience_bullets") or {}
    bullet_text = "\n".join(
        b for blist in bullets.values() for b in (blist or [])
    )
    skills = payload.get("technical_skills") or {}
    skills_text = "\n".join(
        v if isinstance(v, str) else " ".join(v) for v in skills.values()
    )
    return {
        "summary": payload.get("summary") or payload.get("professional_summary") or "",
        "skills": skills_text,
        "bullets": bullet_text,
        "all": "\n".join([payload.get("summary") or "", skills_text, bullet_text]),
    }


def _count_occurrences(canonical_id: str, text: str) -> int:
    """Count word-boundary occurrences of a skill or any of its aliases."""
    index = _surface_index()
    forms = [f for f, cid in index.items() if cid == canonical_id]
    count = 0
    for form in sorted(forms, key=len, reverse=True):
        count += len(re.findall(r"(?i)(?<![\w+#])" + re.escape(form) + r"(?![\w+#])", text))
    return count


def _fuzzy_hit(canonical_id: str, text: str) -> bool:
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return False
    index = _surface_index()
    forms = [f for f, cid in index.items() if cid == canonical_id]
    words = set(re.findall(r"[a-zA-Z][\w+#]*", text.lower()))
    for form in forms:
        for w in words:
            if fuzz.ratio(form.lower(), w) >= FUZZY_THRESHOLD and len(form) > 4:
                return True
    return False


def score_resume(payload: Dict, jd_skills: List[Dict]) -> Dict:
    """Score a tailored resume payload against weighted JD skills."""
    sections = _resume_text(payload)
    all_text = sections["all"]

    matched, missing, stuffing = [], [], []
    total_weight = 0.0
    earned = 0.0

    for skill in jd_skills:
        cid = skill["canonical_id"]
        jd_w = skill.get("weight", 1.0)
        cat_w = CATEGORY_WEIGHTS.get(_category_of(cid), 1.0)
        w = jd_w * cat_w
        total_weight += w

        occurrences = _count_occurrences(cid, all_text)
        if occurrences > 0:
            credit = 1.0
            # evidence bonus: proven in a bullet, not just listed
            if _count_occurrences(cid, sections["bullets"]) > 0:
                credit = 1.1
            earned += w * credit
            matched.append({"skill": cid, "name": skill.get("name"),
                            "occurrences": occurrences, "weight": round(w, 2)})
            if occurrences > STUFFING_LIMIT:
                stuffing.append({"skill": cid, "occurrences": occurrences})
        elif _fuzzy_hit(cid, all_text):
            earned += w * 0.7
            matched.append({"skill": cid, "name": skill.get("name"),
                            "occurrences": 0, "fuzzy": True, "weight": round(w, 2)})
        else:
            missing.append({"skill": cid, "name": skill.get("name"),
                            "weight": round(w, 2)})

    raw = 100.0 * earned / total_weight if total_weight else 0.0
    penalty = STUFFING_PENALTY * len(stuffing)
    score = max(0.0, round(raw - penalty, 1))

    return {
        "score": score,
        "raw_score": round(raw, 1),
        "stuffing_penalty": penalty,
        "matched": sorted(matched, key=lambda m: m["weight"], reverse=True),
        "missing": sorted(missing, key=lambda m: m["weight"], reverse=True),
        "stuffing": stuffing,
        "coverage": round(len(matched) / len(jd_skills), 3) if jd_skills else 0.0,
    }
