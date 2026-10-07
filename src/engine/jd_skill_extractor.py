"""Deterministic JD skill extraction for the Perfect Resume Engine.

No LLM, no embeddings — pure taxonomy matching with section weighting.
The JD is split into sections (requirements / preferred / responsibilities /
other); every taxonomy surface form is matched longest-first so 'PySpark'
never double-counts as 'Spark'; each hit is weighted by its section.

Output: list of dicts sorted by weight desc:
  {canonical_id, name, category, weight, sections, hits}
"""

import logging
import re
from typing import Dict, List

from src.engine.skill_normalizer import _surface_index, canonical_name, load_taxonomy

logger = logging.getLogger(__name__)

SECTION_WEIGHTS = {
    "required": 1.5,
    "preferred": 1.2,
    "responsibilities": 1.0,
    "other": 0.8,
}

_SECTION_PATTERNS = [
    ("required", re.compile(r"(?im)^.*\b(requirements?|must have|required skills|what you.?ll bring|qualifications)\b.*$")),
    ("preferred", re.compile(r"(?im)^.*\b(preferred|nice to have|nice-to-have|bonus|plus\b|desired)\b.*$")),
    ("responsibilities", re.compile(r"(?im)^.*\b(responsibilities|what you.?ll do|role|duties|about the role)\b.*$")),
]


def _split_sections(jd_text: str) -> List[Dict]:
    """Split JD into (section_name, text) chunks by header heuristics."""
    lines = jd_text.splitlines()
    sections: List[Dict] = []
    current = {"name": "other", "lines": []}
    for line in lines:
        matched = None
        for name, pat in _SECTION_PATTERNS:
            if pat.match(line.strip()):
                matched = name
                break
        if matched:
            if current["lines"]:
                sections.append({"name": current["name"], "text": "\n".join(current["lines"])})
            current = {"name": matched, "lines": []}
        else:
            current["lines"].append(line)
    if current["lines"]:
        sections.append({"name": current["name"], "text": "\n".join(current["lines"])})
    return sections or [{"name": "other", "text": jd_text}]


def _compile_patterns():
    """Regex per surface form, longest-first so 'PySpark' wins over 'Spark'."""
    index = _surface_index()
    forms = sorted(index.keys(), key=len, reverse=True)
    return [(re.compile(r"(?i)(?<![\w+#])" + re.escape(f) + r"(?![\w+#])"), index[f]) for f in forms]


_PATTERNS = None


def _patterns():
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = _compile_patterns()
    return _PATTERNS


def extract_jd_skills(jd_text: str) -> List[Dict]:
    """Extract weighted canonical skills from a job description."""
    if not jd_text or not jd_text.strip():
        return []

    agg: Dict[str, Dict] = {}
    for section in _split_sections(jd_text):
        weight = SECTION_WEIGHTS[section["name"]]
        text = section["text"]
        consumed = [False] * len(text)
        for pat, cid in _patterns():
            for m in pat.finditer(text):
                s, e = m.span()
                if any(consumed[s:e]):
                    continue
                for i in range(s, e):
                    consumed[i] = True
                entry = agg.setdefault(
                    cid, {"canonical_id": cid, "name": canonical_name(cid),
                          "category": _category(cid), "weight": 0.0,
                          "sections": set(), "hits": 0}
                )
                # frequency boost with saturation: 1st hit full, later hits +0.2
                entry["weight"] += weight * (1.0 if entry["hits"] == 0 else 0.2)
                entry["sections"].add(section["name"])
                entry["hits"] += 1

    results = sorted(agg.values(), key=lambda r: r["weight"], reverse=True)
    for r in results:
        r["sections"] = sorted(r["sections"])
        r["weight"] = round(r["weight"], 2)
    return results


def _category(canonical_id: str) -> str:
    for skill in load_taxonomy().get("skills", []):
        if skill["id"] == canonical_id:
            return skill["category"]
    return "other"


def skill_ids(jd_text: str) -> List[str]:
    """Convenience: ordered canonical ids only."""
    return [r["canonical_id"] for r in extract_jd_skills(jd_text)]
