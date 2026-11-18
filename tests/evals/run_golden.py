"""Golden-dataset runner — Sprint 4, Lesson 2.

Runs the tailoring graph over every case in
``tests/evals/golden_tailoring.json`` and scores it against the answers
written down in advance. Two modes:

- ``stub`` (default, deterministic, CI-safe): the draft node is stubbed
  to the selected bullets joined — the "perfect drafter". Measures
  retrieve + rank + verify only. Zero LLM cost, fully repeatable.
- ``live`` (de-lab only): a real draft from the MSI's qwen2.5:7b
  (``http://100.68.109.36:11434``). Scores traceability: how much of
  the draft actually comes from the selected bullets.

Scoring, per job case:
- bullets: fraction of the case's expected bullets present in the
  graph's selected bullets (exact text, per slot).
- verdict: the graph's verification.passed must equal
  (expected_verdict == "pass").
- must_not_claim: none of the listed strings may appear in the draft.
- traceability (live mode only): fraction of draft lines whose words
  mostly come from a selected bullet (token containment >= 0.5).

A case passes when bullets == 1.0, verdict matches, and (live mode)
traceability >= 0.8 with no must_not_claim violations. The
draft-adversarial case passes when its fixture draft scores
traceability BELOW 0.5 — the check must catch the invented draft.

Run: python -m tests.evals.run_golden [--mode stub|live]
Exit code 1 when any case fails.
"""

import argparse
import json
import re
import sys
from pathlib import Path

DATASET_PATH = Path(__file__).resolve().parent / "golden_tailoring.json"
MSI_GENERATE_URL = "http://100.68.109.36:11434/api/generate"
TRACEABILITY_LINE_THRESHOLD = 0.5
TRACEABILITY_CASE_THRESHOLD = 0.8


def load_dataset(path=DATASET_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["cases"]


def _words(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def bullet_match_score(expected, selected):
    """Fraction of expected bullets present in the selected set.

    ``expected``/``selected`` are {slot: [bullet text, ...]} dicts.
    Returns (fraction, {slot: fraction}) — a slot with no expected
    bullets counts as 1.0 (nothing to miss).
    """
    per_slot = {}
    total_expected = 0
    total_found = 0
    for slot, want in (expected or {}).items():
        got = set(selected.get(slot, []) or [])
        found = sum(1 for b in want if b in got)
        per_slot[slot] = round(found / len(want), 3) if want else 1.0
        total_expected += len(want)
        total_found += found
    overall = round(total_found / total_expected, 3) if total_expected else 1.0
    return overall, per_slot


def traceability_score(draft, selected_bullets):
    """Fraction of draft claim lines traced to a selected bullet.

    Only bullet lines ("- ..." / "* ...") are claims — headers, role
    lines, and section titles ("**Contact Information**",
    "*Hillwood*") are structure, not claims, and are skipped. A
    claim line counts as traced when at least half of its words
    appear across the selected bullets (token containment against
    their UNION: the model often merges two or three bullets into
    one line, and checking against a single bullet would fail it
    for being faithful). Returns (fraction, [untraced lines]).

    Known limit, owned: a faithful REPHRASE (same meaning, different
    words) still scores low — token overlap cannot see meaning.
    That is Lesson 3's job (RAGAS faithfulness).
    """
    bullets = [b for slot in (selected_bullets or {}).values() for b in slot]
    all_bullet_words = set()
    for b in bullets:
        all_bullet_words |= _words(b)
    lines = [ln.strip() for ln in (draft or "").splitlines() if ln.strip()]
    claims = []
    for ln in lines:
        if not (ln.startswith("- ") or ln.startswith("* ")):
            continue  # header / role line / section title, not a claim
        stripped = ln[2:].strip()
        if "Sri Omkar D" in ln:
            continue
        claims.append(stripped)
    if not claims:
        return 1.0, []
    untraced = []
    for ln in claims:
        words = _words(ln)
        if not words:
            continue
        if len(words & all_bullet_words) / len(words) < TRACEABILITY_LINE_THRESHOLD:
            untraced.append(ln)
    traced = len(claims) - len(untraced)
    return round(traced / len(claims), 3), untraced


def check_must_not_claim(draft, must_not_claim):
    """Strings from must_not_claim found in the draft (case-insensitive)."""
    lowered = (draft or "").lower()
    return [s for s in (must_not_claim or []) if s.lower() in lowered]


def score_draft_case(case, reference_bullets):
    """Score a draft_adversarial fixture: its traceability must be LOW.

    The case passes when the check CATCHES the bad draft — low
    traceability. must_not_claim hits are reported as evidence of the
    catch (the invented strings found), not as failures.
    """
    score, untraced = traceability_score(case["draft"], reference_bullets)
    violations = check_must_not_claim(case["draft"], case.get("must_not_claim", []))
    passed = score < TRACEABILITY_LINE_THRESHOLD
    return {
        "case": case["name"],
        "kind": case["kind"],
        "traceability": score,
        "untraced_lines": len(untraced),
        "must_not_claim_found": violations,
        "passed": passed,
    }


def run_job_case(case, graph_runner, draft_fn=None, score_traceability=False):
    """Run the graph for one job case and score it.

    ``graph_runner`` is run_tailoring_graph (or a stub in tests).
    ``draft_fn`` replaces generate_text when given (stub mode and live
    mode both pass one). ``score_traceability`` is True only in live
    mode: the draft is a real model's words, so how much of it traces
    back to the selected bullets is the question. (The first version
    keyed this on ``draft_fn is None`` and never scored traceability
    in live mode — both modes pass a draft_fn.)
    """
    import src.core.tailoring_graph as tg

    if draft_fn is not None:
        original = tg.generate_text
        tg.generate_text = draft_fn
        try:
            out = graph_runner(case["job_id"])
        finally:
            tg.generate_text = original
    else:
        out = graph_runner(case["job_id"])
    selected = out.get("selected_bullets") or {}
    bullets, per_slot = bullet_match_score(case.get("expected_selected_bullets"), selected)
    verdict_ok = (out.get("verification") or {}).get("passed", False) == (
        case.get("expected_verdict") == "pass"
    )
    draft = out.get("draft") or ""
    violations = check_must_not_claim(draft, case.get("must_not_claim", []))
    result = {
        "case": case["name"],
        "kind": case["kind"],
        "bullets": bullets,
        "per_slot": per_slot,
        "verdict_ok": verdict_ok,
        "verification": out.get("verification"),
        "draft_attempts": out.get("draft_attempts"),
        "error": out.get("error"),
        "must_not_claim_found": violations,
    }
    if score_traceability:
        trace, untraced = traceability_score(draft, selected)
        result["traceability"] = trace
        result["untraced_lines"] = untraced[:5]
        result["passed"] = (
            bullets == 1.0
            and verdict_ok
            and not violations
            and trace >= TRACEABILITY_CASE_THRESHOLD
            and not out.get("error")
        )
    else:
        result["passed"] = bullets == 1.0 and verdict_ok and not violations and not out.get("error")
    return result


def stub_draft(prompt, **kwargs):
    """Mode A draft: the selected bullets, and only the selected bullets.

    The prompt carries the bullets as "- " lines under the slot
    headers; the stub lifts exactly those lines (stopping at the
    acronym rule or the style instruction) and prepends the candidate
    name, which verify requires. The perfect drafter: nothing added,
    nothing invented.
    """
    lines = ["Sri Omkar D"]
    for ln in prompt.splitlines():
        if ln.startswith(("Write plainly.", "ACRONYM RULE")):
            break
        if ln.startswith("- "):
            lines.append(ln)
    return "\n".join(lines)


def live_draft(prompt, **kwargs):
    """Mode B draft: the MSI's qwen2.5:7b, same call as Lesson 1's Run B."""
    import urllib.request

    body = json.dumps(
        {
            "model": "qwen2.5:7b",
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},
        }
    ).encode()
    req = urllib.request.Request(
        MSI_GENERATE_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())["response"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["stub", "live"], default="stub")
    parser.add_argument("--cases", nargs="*", default=None, help="case names to run")
    args = parser.parse_args(argv)
    cases = load_dataset()
    if args.cases:
        cases = [c for c in cases if c["name"] in args.cases]
    from src.core.tailoring_graph import run_tailoring_graph

    by_name = {c["name"]: c for c in cases}
    results = []
    for case in cases:
        if case["kind"] == "draft_adversarial":
            ref = by_name[case["trace_against"]]["expected_selected_bullets"]
            results.append(score_draft_case(case, ref))
            continue
        draft_fn = stub_draft if args.mode == "stub" else live_draft
        results.append(
            run_job_case(
                case,
                run_tailoring_graph,
                draft_fn=draft_fn,
                score_traceability=(args.mode == "live"),
            )
        )
    passed = sum(1 for r in results if r["passed"])
    print(f"mode: {args.mode}  cases: {len(results)}  passed: {passed}")
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        detail = {k: v for k, v in r.items() if k not in ("case", "kind", "passed")}
        print(f"[{mark}] {r['case']}: {json.dumps(detail)}")
    if passed != len(results):
        print(f"\n{len(results) - passed} case(s) below bar.")
        return 1
    print("\nAll golden cases pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
