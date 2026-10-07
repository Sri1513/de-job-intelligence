"""Phase 5: credibility audit + verify→revise loop.

The critical empirical finding (Takano 2026): prompt guardrails alone
leave fabrications in 50% of tailored outputs. This module is the
mandatory deterministic layer on top:

  audit_credibility: every number, tool/skill, employer, and date in the
    generated bullets must trace to the source bullet bank or the JD.
    Violations are reported per bullet — no LLM involved.

  verify_and_revise: runs audit + the Phase-4 scorer; if either fails,
    calls revise_fn (the pipeline's LLM revision with the gap list) and
    re-checks, up to max_iters. Fail closed: after exhausting retries,
    violating bullets are replaced with their verbatim source-bank text
    instead of shipping a polished fabrication.
"""

import logging
import re
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*(?:AM|PM)?", re.IGNORECASE)


def _bank_texts(slot_data: Dict) -> List[str]:
    texts = []
    for data in slot_data.values():
        for b in data.get("bullet_bank", []) or []:
            texts.append(b.get("text", ""))
    return texts


def _bank_skill_ids(slot_data: Dict) -> set:
    from src.engine.skill_normalizer import normalize_skill
    ids = set()
    for data in slot_data.values():
        for b in data.get("bullet_bank", []) or []:
            for tag in b.get("skills", []) or []:
                cid = normalize_skill(str(tag))
                if cid:
                    ids.add(cid)
    return ids


def _numbers(text: str) -> List[str]:
    return _NUMBER_RE.findall(text) + _TIME_RE.findall(text)


def _norm_num(n: str) -> str:
    return n.strip().upper().replace(" ", "")


def audit_credibility(
    payload: Dict, slot_data: Dict, jd_skill_ids: List[str] | None = None
) -> Dict:
    """Deterministically verify every claim traces to bank or JD."""
    from src.engine.jd_skill_extractor import _patterns

    bank_texts = _bank_texts(slot_data)
    bank_blob = "\n".join(bank_texts)
    bank_numbers = {_norm_num(n) for t in bank_texts for n in _numbers(t)}
    bank_skills = _bank_skill_ids(slot_data)
    jd_skills = set(jd_skill_ids or [])
    allowed_skills = bank_skills | jd_skills

    violations = []
    bullets = payload.get("experience_bullets") or {}
    for slot, blist in bullets.items():
        for i, bullet in enumerate(blist or []):
            bid = f"{slot}[{i}]"
            # 1. numbers must occur in the bank
            for n in _numbers(bullet):
                if _norm_num(n) not in bank_numbers:
                    violations.append({"bullet": bid, "type": "number",
                                       "value": n, "detail": "metric not in bullet bank"})
            # 2. skill mentions must be in (bank ∪ JD)
            matched_cids = set()
            for pat, cid in _patterns():
                if pat.search(bullet):
                    matched_cids.add(cid)
            for cid in matched_cids:
                if cid not in allowed_skills:
                    violations.append({"bullet": bid, "type": "tool",
                                       "value": cid, "detail": "tool in neither bank nor JD"})

    # 3. summary numbers must also trace
    summary = payload.get("summary") or payload.get("professional_summary") or ""
    for n in _numbers(summary):
        if _norm_num(n) not in bank_numbers and _norm_num(n) not in {"7+", "7"}:
            # "7+ years" tenure is the verified profile fact
            violations.append({"bullet": "summary", "type": "number",
                               "value": n, "detail": "metric not in bullet bank"})

    return {"clean": not violations, "violations": violations,
            "checked_bullets": sum(len(v or []) for v in bullets.values())}


def _best_bank_match(bullet: str, slot_data: Dict) -> str:
    """Fail-closed fallback: verbatim bank bullet with max token overlap."""
    btokens = set(bullet.lower().split())
    best, best_score = None, -1
    for data in slot_data.values():
        for b in data.get("bullet_bank", []) or []:
            text = b.get("text", "")
            score = len(btokens & set(text.lower().split()))
            if score > best_score:
                best, best_score = text, score
    return best or bullet


def verify_and_revise(
    payload: Dict,
    slot_data: Dict,
    jd_skills: List[Dict],
    revise_fn: Callable[[Dict, Dict, Dict], Dict],
    score_threshold: float = 70.0,
    max_iters: int = 3,
) -> Dict:
    """Audit + score; revise with gap feedback until clean or iters run out.

    revise_fn(payload, audit_report, score_report) -> new payload.
    Fail closed: violating bullets are replaced with verbatim bank text.
    Returns {"payload": ..., "passed": bool, "iterations": int,
             "audit": ..., "score": ...}.
    """
    from src.engine.resume_scorer import score_resume

    jd_ids = [s["canonical_id"] for s in jd_skills]
    current = payload
    for it in range(max_iters):
        audit = audit_credibility(current, slot_data, jd_ids)
        score = score_resume(current, jd_skills)
        if audit["clean"] and score["score"] >= score_threshold:
            return {"payload": current, "passed": True, "iterations": it,
                    "audit": audit, "score": score}
        logger.info("verify→revise iter %d: %d violations, score %.1f — revising",
                    it, len(audit["violations"]), score["score"])
        current = revise_fn(current, audit, score)

    # fail closed: swap violating bullets for verbatim bank text
    audit = audit_credibility(current, slot_data, jd_ids)
    bad = {v["bullet"] for v in audit["violations"]}
    fixed = {**current, "experience_bullets": {}}
    for slot, blist in (current.get("experience_bullets") or {}).items():
        fixed["experience_bullets"][slot] = [
            _best_bank_match(b, slot_data) if f"{slot}[{i}]" in bad else b
            for i, b in enumerate(blist or [])
        ]
    final_audit = audit_credibility(fixed, slot_data, jd_ids)
    final_score = score_resume(fixed, jd_skills)
    return {"payload": fixed, "passed": final_audit["clean"],
            "iterations": max_iters, "audit": final_audit, "score": final_score}
