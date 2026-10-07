"""Deterministic bullet ranking for the Perfect Resume Engine.

Scores each bullet in a slot's bank by weighted canonical-skill overlap
with the JD's extracted skills. No LLM, no vibes — the same canonical ids
on both sides, so 'PySpark' in the JD matches 'pyspark' in a bullet tag.

This is the Phase-2 deterministic signal. Phase 3 adds BM25+SBERT hybrid
scoring on top; the overlap score remains the lexical anchor.
"""

import logging
from typing import Dict, List

from src.engine.skill_normalizer import normalize_skill

logger = logging.getLogger(__name__)


def _bullet_skill_ids(bullet: Dict) -> List[str]:
    ids = []
    for tag in bullet.get("skills", []) or []:
        cid = normalize_skill(str(tag))
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def rank_slot_bullets(bullet_bank: List[Dict], jd_skills: List[Dict]) -> List[Dict]:
    """Rank a slot's bullet bank against weighted JD skills.

    jd_skills: output of extract_jd_skills (canonical_id + weight).
    Returns the bank sorted by overlap score desc, each bullet annotated
    with _overlap_score and _matched_skills (canonical ids).
    """
    jd_weights = {s["canonical_id"]: s["weight"] for s in jd_skills}
    ranked = []
    for bullet in bullet_bank:
        bids = _bullet_skill_ids(bullet)
        matched = [cid for cid in bids if cid in jd_weights]
        score = round(sum(jd_weights[cid] for cid in matched), 2)
        ranked.append({**bullet, "_overlap_score": score, "_matched_skills": matched})
    ranked.sort(key=lambda b: b["_overlap_score"], reverse=True)
    return ranked


def rank_all_slots(slot_data: Dict[str, Dict], jd_text: str) -> Dict[str, List[Dict]]:
    """Rank every slot's bank; returns {slot: ranked_bullets}."""
    from src.engine.jd_skill_extractor import extract_jd_skills

    jd_skills = extract_jd_skills(jd_text or "")
    return {
        slot: rank_slot_bullets(data.get("bullet_bank", []), jd_skills)
        for slot, data in slot_data.items()
    }
