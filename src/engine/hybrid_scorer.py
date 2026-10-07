"""Phase 3: hybrid bullet scoring + coverage/MMR selection.

Signals per (bullet, JD):
  1. canonical overlap (Phase 2) — weighted exact skill-id matching
  2. BM25 — lexical match of bullet text against JD skill terms
  3. SBERT cosine — semantic similarity (optional; skipped gracefully when
     sentence-transformers or the model is unavailable)

Fusion: Reciprocal Rank Fusion (k=60) over the available rankings.
Selection: greedy max-coverage over weighted JD skills ((1-1/e) approx),
then MMR (lambda=0.7) for diversity into K slots.
"""

import logging
import math
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

RRF_K = 60
MMR_LAMBDA = 0.7

_sbert_model = None
_sbert_failed = False


def _get_sbert():
    global _sbert_model, _sbert_failed
    if _sbert_model is not None:
        return _sbert_model
    if _sbert_failed:
        return None
    try:
        from sentence_transformers import SentenceTransformer
        _sbert_model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("SBERT model loaded for hybrid scoring")
    except Exception as e:  # pragma: no cover - environment dependent
        logger.warning("SBERT unavailable, hybrid scoring without dense signal: %s", e)
        _sbert_failed = True
        return None
    return _sbert_model


def bm25_scores(bullet_texts: List[str], query_terms: List[str]) -> List[float]:
    """Lexical relevance of each bullet to the JD's skill terms."""
    if not bullet_texts or not query_terms:
        return [0.0] * len(bullet_texts)
    from rank_bm25 import BM25Okapi
    tokenized = [t.lower().split() for t in bullet_texts]
    bm25 = BM25Okapi(tokenized)
    return [float(s) for s in bm25.get_scores([q.lower() for q in query_terms])]


def sbert_scores(bullet_texts: List[str], jd_text: str) -> Optional[List[float]]:
    """Semantic similarity of each bullet to the JD; None if model unavailable."""
    model = _get_sbert()
    if model is None or not bullet_texts or not jd_text:
        return None
    import numpy as np
    bullet_embs = model.encode(bullet_texts, normalize_embeddings=True)
    jd_emb = model.encode([jd_text], normalize_embeddings=True)[0]
    return [float(np.dot(e, jd_emb)) for e in bullet_embs]


def rrf_fuse(rankings: List[List[float]], k: int = RRF_K) -> List[float]:
    """Reciprocal Rank Fusion over parallel score lists (higher score = better)."""
    n = len(rankings[0])
    fused = [0.0] * n
    for scores in rankings:
        order = sorted(range(n), key=lambda i: scores[i], reverse=True)
        for rank, idx in enumerate(order, start=1):
            fused[idx] += 1.0 / (k + rank)
    return fused


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def greedy_coverage(
    bullets: List[Dict], jd_weights: Dict[str, float], k: int
) -> List[Dict]:
    """Greedily pick k bullets maximizing marginal weighted skill coverage."""
    remaining = list(bullets)
    selected: List[Dict] = []
    covered: Dict[str, float] = {}
    while remaining and len(selected) < k:
        best, best_gain = None, -1.0
        for b in remaining:
            gain = sum(
                w for cid, w in jd_weights.items()
                if cid in b.get("_skill_ids", []) and cid not in covered
            )
            # tie-break: higher fused score wins
            gain += 1e-6 * b.get("_fused_score", 0.0)
            if gain > best_gain:
                best, best_gain = b, gain
        if best is None or best_gain <= 0:
            break
        selected.append(best)
        remaining.remove(best)
        for cid in best.get("_skill_ids", []):
            covered[cid] = jd_weights.get(cid, 0.0)
    return selected


def mmr_select(
    bullets: List[Dict], k: int, lambda_mult: float = MMR_LAMBDA
) -> List[Dict]:
    """Maximal Marginal Relevance: relevance vs. redundancy to selected set."""
    remaining = list(bullets)
    selected: List[Dict] = []
    while remaining and len(selected) < k:
        best, best_val = None, -1e9
        for b in remaining:
            redundancy = max(
                (_jaccard(b.get("text", ""), s.get("text", "")) for s in selected),
                default=0.0,
            )
            val = lambda_mult * b.get("_fused_score", 0.0) - (1 - lambda_mult) * redundancy
            if val > best_val:
                best, best_val = b, val
        selected.append(best)
        remaining.remove(best)
    return selected


def score_and_select(
    bullet_bank: List[Dict],
    jd_text: str,
    jd_skills: List[Dict],
    k: int,
) -> List[Dict]:
    """Full Phase-3 pipeline: hybrid score -> coverage -> MMR -> top-k bullets."""
    from src.engine.bullet_ranker import _bullet_skill_ids

    texts = [b.get("text", "") for b in bullet_bank]
    query_terms = [s["canonical_id"].replace("_", " ") for s in jd_skills]
    query_terms += [s["name"] for s in jd_skills]

    overlap = [
        sum(s["weight"] for s in jd_skills if s["canonical_id"] in _bullet_skill_ids(b))
        for b in bullet_bank
    ]
    bm25 = bm25_scores(texts, query_terms)
    dense = sbert_scores(texts, jd_text or "")

    rankings = [overlap, bm25] + ([dense] if dense else [])
    fused = rrf_fuse(rankings)

    enriched = []
    for b, f in zip(bullet_bank, fused):
        enriched.append({**b, "_fused_score": round(f, 4),
                         "_skill_ids": _bullet_skill_ids(b)})

    jd_weights = {s["canonical_id"]: s["weight"] for s in jd_skills}
    covered = greedy_coverage(enriched, jd_weights, k)
    # MMR over coverage winners + next-best by fused score for diversity
    pool_ids = {id(b) for b in covered}
    pool = covered + [b for b in sorted(enriched, key=lambda x: x["_fused_score"], reverse=True)
                      if id(b) not in pool_ids]
    return mmr_select(pool[: max(k * 2, k)], k)
