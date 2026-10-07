"""Phase 6: eval harness with a fixed benchmark.

Freezes a set of (JD, expectations) pairs and measures the deterministic
engine on every run — no LLM spend. Axes (from the research):
  1. slot-routing accuracy (healthcare -> optum, else herc_rentals)
  2. extractor recall (required skills found / required skills)
  3. scorer calibration (good payload outscores bad payload)
  4. fabrication detection (audit flags the planted violations)

Run: python -m src.engine.eval_harness
Benchmark lives in eval/benchmark.json; add cases there as JDs arrive.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

BENCHMARK_PATH = Path(__file__).resolve().parent.parent.parent / "eval" / "benchmark.json"


def _load_benchmark() -> List[Dict]:
    return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))["cases"]


def _route_job1(jd_text: str, mock_llm_framework: str | None = None) -> str:
    """Route slot 1's framework. When mock_llm_framework is given, the
    selector's LLM call is stubbed with it (no quota burn); this validates
    the wiring (LLM says optum -> slot1=optum), not the model's judgment.
    Thin inputs never reach the LLM at all."""
    from unittest.mock import patch

    import src.engine.framework_selector as sel

    if mock_llm_framework:
        fake = json.dumps({"framework": mock_llm_framework,
                           "rationale": "benchmark mock"})
        with patch.object(sel, "generate_text", return_value=fake):
            slots = sel.select_slot_frameworks(jd_text, "Data Engineer")
    else:
        slots = sel.select_slot_frameworks(jd_text, "Data Engineer")
    return slots["job1"]


def run_benchmark() -> Dict:
    from src.engine.credibility_audit import audit_credibility
    from src.engine.jd_skill_extractor import extract_jd_skills, skill_ids
    from src.engine.resume_scorer import score_resume

    cases = _load_benchmark()
    results = []
    for case in cases:
        jd = case["jd"]
        r: Dict = {"case": case["name"], "checks": {}}

        # 1. slot routing (LLM stubbed per case — validates wiring, not judgment)
        if "expected_job1" in case:
            routed = _route_job1(jd, case.get("mock_llm_framework"))
            r["checks"]["routing"] = routed == case["expected_job1"]

        # 2. extractor recall
        if "must_extract" in case:
            found = set(skill_ids(jd))
            need = set(case["must_extract"])
            r["checks"]["recall"] = round(len(found & need) / len(need), 3) if need else 1.0

        # 3. scorer calibration
        if "good_payload" in case:
            jd_skills = extract_jd_skills(jd)
            good = score_resume(case["good_payload"], jd_skills)["score"]
            bad = score_resume(case["bad_payload"], jd_skills)["score"]
            r["checks"]["scorer_good_gt_bad"] = good > bad
            r["checks"]["good_score"] = good

        # 4. fabrication detection
        if "tainted_payload" in case:
            audit = audit_credibility(
                case["tainted_payload"], case["slot_data"],
                case.get("jd_skill_ids", []))
            r["checks"]["audit_clean"] = not audit["clean"]
            r["checks"]["violations_found"] = len(audit["violations"])

        results.append(r)

    summary = {
        "cases": len(results),
        "routing_acc": _mean([r["checks"].get("routing") for r in results]),
        "mean_recall": _mean([r["checks"].get("recall") for r in results]),
        "scorer_calibrated": all(r["checks"].get("scorer_good_gt_bad", True) for r in results),
        "audit_catches": all(r["checks"].get("audit_clean", True) for r in results),
    }
    return {"results": results, "summary": summary}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 3)


def main():
    logging.basicConfig(level=logging.WARNING)
    report = run_benchmark()
    print(json.dumps(report["summary"], indent=2))
    failed = [r for r in report["results"]
              if any(v is False or (isinstance(v, float) and v < 0.8)
                     for v in r["checks"].values())]
    if failed:
        print(f"\n{len(failed)} case(s) below bar:")
        for r in failed:
            print(f"  - {r['case']}: {r['checks']}")
        sys.exit(1)
    print("\nAll benchmark cases pass.")


if __name__ == "__main__":
    main()
