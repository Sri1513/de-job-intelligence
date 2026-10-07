"""Canonical skill normalization for the Perfect Resume Engine.

Every skill surface form — JD text, bullet-bank tags — resolves to one
canonical taxonomy id *before* any matching happens, so 'PySpark' vs
'pyspark' vs 'Spark (Python)' score as a single skill.

Resolution order (cheap → expensive):
  1. exact canonical id or name match (case-insensitive)
  2. alias map lookup
  3. rapidfuzz fuzzy match against all known surface forms (>= 90)

Unmatched spans return None — callers keep them as free text rather than
dropping them, because a dropped skill is a false negative downstream.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TAXONOMY_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "skills_taxonomy.json"

FUZZY_THRESHOLD = 90


@lru_cache(maxsize=1)
def load_taxonomy() -> Dict:
    with open(TAXONOMY_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _surface_index() -> Dict[str, str]:
    """Maps every known surface form (lowercased) -> canonical id."""
    index: Dict[str, str] = {}
    for skill in load_taxonomy().get("skills", []):
        cid = skill["id"]
        index[cid.lower()] = cid
        index[skill["name"].lower()] = cid
        for alias in skill.get("aliases", []):
            index[alias.lower()] = cid
    return index


def normalize_skill(text: str) -> Optional[str]:
    """Resolve a raw skill string to its canonical taxonomy id, or None."""
    if not text:
        return None
    key = text.strip().lower()
    index = _surface_index()

    # 1. exact
    if key in index:
        return index[key]

    # 2. fuzzy (typos, close variants: 'Kubernates' -> kubernetes)
    try:
        from rapidfuzz import process as fuzz_process
    except ImportError:  # pragma: no cover
        logger.warning("rapidfuzz not installed; skipping fuzzy skill match")
        return None

    match = fuzz_process.extractOne(key, list(index.keys()))
    if match and match[1] >= FUZZY_THRESHOLD:
        return index[match[0]]
    return None


def normalize_skills(texts: List[str]) -> Dict[str, Optional[str]]:
    """Batch-normalize; returns {raw_text: canonical_id | None}."""
    return {t: normalize_skill(t) for t in texts}


def canonical_name(canonical_id: str) -> Optional[str]:
    """Display name for a canonical id."""
    for skill in load_taxonomy().get("skills", []):
        if skill["id"] == canonical_id:
            return skill["name"]
    return None
