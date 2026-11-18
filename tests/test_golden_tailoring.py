"""Tests for the golden-dataset runner (Sprint 4, Lesson 2).

The scoring functions are tested with fake data — no DB, no LLM.
The dataset file itself is validated structurally: 25 cases, unique
names, three real bullets per slot per job case, and the draft
fixture present. The live run (real graph, real DB) happens on
de-lab via `python -m tests.evals.run_golden`, not here.
"""

import json
import re
from pathlib import Path

import pytest

from tests.evals.run_golden import (
    bullet_match_score,
    check_must_not_claim,
    run_job_case,
    score_draft_case,
    traceability_score,
)

DATASET = Path(__file__).resolve().parent / "evals" / "golden_tailoring.json"

BANNED_WORDS = (
    "leveraging",
    "spearheaded",
    "mastered",
    "passionate",
    "cutting-edge",
    "robust",
    "seamless",
    "rigorous",
    "state-of-the-art",
)
ABSOLUTE_PATTERNS = (
    r"100\s*%",
    r"0\s*%\s*data loss",
    r"zero\s+data loss",
    r"zero\s+downtime",
    r"guarantee",
)


def _load():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_bullet_match_full_and_partial():
    expected = {"job1": ["a", "b", "c"], "job2": ["d", "e", "f"]}
    assert bullet_match_score(expected, expected)[0] == 1.0
    partial = {"job1": ["a", "b"], "job2": ["d", "e", "f"]}
    score, per_slot = bullet_match_score(expected, partial)
    assert score == round(5 / 6, 3)
    assert per_slot["job1"] == round(2 / 3, 3)
    assert per_slot["job2"] == 1.0


def test_bullet_match_empty_expected_counts_as_one():
    assert bullet_match_score({}, {"job1": ["a"]}) == (1.0, {})


def test_traceability_traced_and_invented_lines():
    bullets = {"job1": ["Built pipelines with PySpark and Databricks for daily loads"]}
    draft = (
        "Sri Omkar D\n"
        "- Built pipelines with PySpark and Databricks for daily loads\n"
        "- Data Analyst at XYZ Corporation for three years\n"
    )
    score, untraced = traceability_score(draft, bullets)
    assert score == 0.5
    assert untraced == ["Data Analyst at XYZ Corporation for three years"]


def test_traceability_skips_headers_and_name():
    bullets = {"job1": ["Built pipelines with PySpark"]}
    draft = "### Sri Omkar D. - Data Engineer\n[ job1 ]\n- Built pipelines with PySpark\n"
    score, untraced = traceability_score(draft, bullets)
    assert score == 1.0
    assert untraced == []


def test_traceability_merged_line_traces_via_union():
    # The model often merges two bullets into one line. Containment
    # is measured against the UNION of the selected bullets, so a
    # merged line traces even though no single bullet covers it.
    bullets = {
        "job1": [
            "Replaced full-refresh cycles with scheduled incremental load patterns",
            "Tuned partition strategies and optimized Spark SQL joins",
        ]
    }
    draft = (
        "**Senior Data Engineer**\n"
        "- Replaced full-refresh cycles with scheduled incremental load patterns, "
        "tuned partition strategies and optimized Spark SQL joins.\n"
        "- Email: fake@example.com\n"
    )
    score, untraced = traceability_score(draft, bullets)
    assert score == 0.5
    assert untraced == ["Email: fake@example.com"]


def test_run_job_case_scores_traceability_in_live_mode():
    # Regression: the first runner keyed traceability on
    # ``draft_fn is None`` and never scored it in live mode — both
    # modes pass a draft_fn. It must be keyed on the mode flag.
    case = {
        "name": "fake",
        "job_id": "fake-1",
        "kind": "positive",
        "expected_selected_bullets": {"job1": ["Built pipelines with PySpark"]},
        "expected_verdict": "pass",
        "must_not_claim": [],
    }

    def fake_graph(job_id):
        return {
            "selected_bullets": {"job1": ["Built pipelines with PySpark"]},
            "draft": "Sri Omkar D\n- Built pipelines with PySpark\n",
            "verification": {"passed": True, "failures": []},
            "draft_attempts": 1,
            "error": None,
        }

    result = run_job_case(
        case,
        fake_graph,
        draft_fn=lambda prompt, **kw: prompt,
        score_traceability=True,
    )
    assert result["traceability"] == 1.0
    assert result["passed"] is True
    stub_result = run_job_case(
        case,
        fake_graph,
        draft_fn=lambda prompt, **kw: prompt,
        score_traceability=False,
    )
    assert "traceability" not in stub_result


def test_must_not_claim_finds_invented_strings():
    draft = "Worked at XYZ Corporation and ABC Consulting."
    assert check_must_not_claim(draft, ["XYZ Corporation", "Acme"]) == ["XYZ Corporation"]
    assert check_must_not_claim(draft, ["Acme"]) == []


def test_draft_adversarial_case_fails_traceability():
    data = _load()
    draft_case = next(c for c in data["cases"] if c["kind"] == "draft_adversarial")
    ref = next(c for c in data["cases"] if c["name"] == draft_case["trace_against"])
    result = score_draft_case(draft_case, ref["expected_selected_bullets"])
    assert result["passed"] is True  # the check CATCHES the bad draft
    assert result["traceability"] < 0.5
    assert "XYZ Corporation" in result["must_not_claim_found"]
    assert "ABC Consulting" in result["must_not_claim_found"]


def test_dataset_shape():
    data = _load()
    assert data["version"] == 1
    cases = data["cases"]
    assert len(cases) == 25
    names = [c["name"] for c in cases]
    assert len(set(names)) == len(names)
    kinds = [c["kind"] for c in cases]
    assert kinds.count("positive") == 20
    assert kinds.count("adversarial") == 4
    assert kinds.count("draft_adversarial") == 1


def test_dataset_job_cases_have_three_real_bullets_per_slot():
    data = _load()
    for case in data["cases"]:
        if case["kind"] == "draft_adversarial":
            continue
        assert case["job_id"], case["name"]
        expected = case["expected_selected_bullets"]
        assert set(expected) == {"job1", "job2", "job3", "job4"}, case["name"]
        for slot, bullets in expected.items():
            assert len(bullets) == 3, (case["name"], slot)
            assert all(b.strip() for b in bullets), (case["name"], slot)


def test_dataset_expected_bullets_contain_no_banned_words_or_absolutes():
    data = _load()
    for case in data["cases"]:
        if case["kind"] == "draft_adversarial":
            continue
        for slot, bullets in case["expected_selected_bullets"].items():
            for b in bullets:
                low = b.lower()
                for word in BANNED_WORDS:
                    assert not re.search(r"\b" + re.escape(word) + r"\b", low), (
                        case["name"],
                        slot,
                        word,
                    )
                for pattern in ABSOLUTE_PATTERNS:
                    assert not re.search(pattern, low), (case["name"], slot, pattern)
                assert "Microsoft Certified Professional" not in b


def test_dataset_draft_case_references_a_real_case():
    data = _load()
    draft_case = next(c for c in data["cases"] if c["kind"] == "draft_adversarial")
    assert draft_case["draft"].strip()
    assert draft_case["expected_verdict"] == "fail"
    assert draft_case["trace_against"] in {c["name"] for c in data["cases"]}
    assert draft_case["must_not_claim"]
