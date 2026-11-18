# tests/test_tailoring_graph.py — Sprint 4, Lesson 1.
# The four-node tailoring graph (src/core/tailoring_graph.py). The LLM
# and the builder are stubbed; no model, no DB, no network.
import pytest

from src.core.tailoring_graph import (
    CANDIDATE_CONTACT,
    MAX_DRAFT_ATTEMPTS,
    TOP_BULLETS_PER_SLOT,
    _build_draft_prompt,
    build_tailoring_graph,
    draft_node,
    rank_node,
    retrieve_node,
    route_after_retrieve,
    route_after_verify,
    run_tailoring_graph,
    verify_node,
)

CLEAN_DRAFT = (
    "Sri Omkar D\n"
    "Data Visualization Engineer - 4P Consulting\n"
    "Built Power BI dashboards on Databricks SQL for store sell-through reporting. "
    "Created PySpark pipelines in Databricks Delta Lake."
)

BAD_DRAFT = (
    "Sri Omkar D\n"
    "Leveraging cutting-edge dashboards, I delivered 100% data accuracy "
    "with zero downtime, guarantee."
)


def _bundle():
    # The REAL bundle shape: ranking entries carry only
    # {id, score, matched}; the text lives in the slot's bullet_bank
    # (dynamic_tool_bridging_policy.slot_frameworks). Lesson 1's test
    # used a fake with "text" on the ranking entries and never caught
    # that rank read a key that does not exist.
    banks = {
        "job1": [{"id": f"job1-{i}", "text": f"job1 bullet {i}"} for i in range(5)],
        "job2": [{"id": f"job2-{i}", "text": f"job2 bullet {i}"} for i in range(5)],
    }
    return {
        "job_metadata": {"title": "Data Engineer", "company": "Acme"},
        "tailoring_guidelines": {
            "deterministic_bullet_ranking": {
                slot: [{"id": b["id"], "score": 5.0 - i, "matched": []} for i, b in enumerate(bank)]
                for slot, bank in banks.items()
            },
            "dynamic_tool_bridging_policy": {
                "slot_frameworks": {slot: {"bullet_bank": bank} for slot, bank in banks.items()}
            },
            "acronym_rule": "ACRONYM RULE: MCP = Model Context Protocol.",
        },
    }


def _state(**overrides):
    state = {"job_id": "j1", "bundle": _bundle()}
    state.update(overrides)
    return state


# --- verify_node: the cheap checks ---


def test_verify_passes_clean_draft():
    result = verify_node(_state(draft=CLEAN_DRAFT))
    assert result["verification"] == {"passed": True, "failures": []}


def test_verify_flags_banned_words():
    result = verify_node(_state(draft=BAD_DRAFT))
    failures = result["verification"]["failures"]
    assert result["verification"]["passed"] is False
    assert "banned word: leveraging" in failures
    assert "banned word: cutting-edge" in failures


def test_verify_flags_absolute_claims():
    result = verify_node(_state(draft=BAD_DRAFT))
    failures = result["verification"]["failures"]
    assert any("absolute claim" in f for f in failures)
    assert len([f for f in failures if "absolute claim" in f]) == 3


def test_verify_flags_missing_name_and_mcp_expansion():
    draft = "Built dashboards. Microsoft Certified Professional required."
    result = verify_node(_state(draft=draft))
    failures = result["verification"]["failures"]
    assert "missing candidate name: Sri Omkar D" in failures
    assert "forbidden acronym expansion: Microsoft Certified Professional" in failures


def test_verify_no_draft_is_a_failure():
    result = verify_node(_state())
    assert result["verification"]["passed"] is False
    assert result["verification"]["failures"] == ["no draft to verify"]


# --- rank_node ---


def test_rank_keeps_top_three_per_slot():
    assert TOP_BULLETS_PER_SLOT == 3
    result = rank_node(_state())
    assert result["selected_bullets"] == {
        "job1": ["job1 bullet 0", "job1 bullet 1", "job1 bullet 2"],
        "job2": ["job2 bullet 0", "job2 bullet 1", "job2 bullet 2"],
    }


def test_rank_resolves_ids_and_never_emits_empty_strings():
    # Regression: the real bundle's ranking entries have no "text" key
    # (only id/score/matched). A ranked id missing from the bank must
    # be skipped, never selected as "".
    state = _state()
    state["bundle"]["tailoring_guidelines"]["deterministic_bullet_ranking"]["job1"] = [
        {"id": "job1-0", "score": 5.0, "matched": []},
        {"id": "ghost-id", "score": 4.0, "matched": []},
        {"id": "job1-1", "score": 3.0, "matched": []},
    ]
    result = rank_node(state)
    assert result["selected_bullets"]["job1"] == ["job1 bullet 0", "job1 bullet 1"]
    assert all(text for slot in result["selected_bullets"].values() for text in slot)


def test_rank_noop_when_retrieve_failed():
    assert rank_node(_state(error="retrieve failed: boom")) == {}


# --- retrieve_node + routing ---


def test_retrieve_failure_sets_error(monkeypatch):
    def _boom(job_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.core.tailoring_graph.build_job_tailoring_prompt", _boom)
    result = retrieve_node({"job_id": "j1"})
    assert result["error"] == "retrieve failed: db down"
    assert route_after_retrieve(result) == "done"
    assert route_after_retrieve({"job_id": "j1", "bundle": {}}) == "rank"


def test_route_after_verify():
    assert route_after_verify({"verification": {"passed": True}}) == "done"
    assert route_after_verify({"error": "boom"}) == "done"
    assert (
        route_after_verify(
            {"verification": {"passed": False}, "draft_attempts": MAX_DRAFT_ATTEMPTS}
        )
        == "done"
    )
    assert (
        route_after_verify(
            {"verification": {"passed": False, "failures": ["x"]}, "draft_attempts": 1}
        )
        == "draft"
    )


# --- draft prompt ---


def test_draft_prompt_carries_real_contact_block():
    state = {
        "bundle": {
            "job_metadata": {"title": "Data Engineer", "company": "4P"},
            "tailoring_guidelines": {},
        },
        "selected_bullets": {"job1": ["Built pipelines with PySpark"]},
    }
    prompt = _build_draft_prompt(state)
    assert CANDIDATE_CONTACT in prompt
    assert "Never invent an email" in prompt
    assert "sriomkar@example.com" not in prompt


def test_draft_prompt_includes_failures_on_retry():
    prompt = _build_draft_prompt(
        _state(
            selected_bullets={"job1": ["b1"]},
            verification={"passed": False, "failures": ["banned word: leveraging"]},
            draft_attempts=1,
        )
    )
    assert "FAILED verification" in prompt
    assert "banned word: leveraging" in prompt
    assert "job1 bullet" not in prompt  # only the SELECTED bullets, not the bank
    assert "b1" in prompt


def test_draft_prompt_first_attempt_has_no_failure_section():
    prompt = _build_draft_prompt(_state(selected_bullets={"job1": ["b1"]}))
    assert "FAILED verification" not in prompt


# --- full graph ---


def test_graph_retries_once_then_passes(monkeypatch):
    monkeypatch.setattr(
        "src.core.tailoring_graph.build_job_tailoring_prompt",
        lambda job_id: _bundle(),
    )
    calls = {"n": 0}

    def _stub(prompt, **kwargs):
        calls["n"] += 1
        return BAD_DRAFT if calls["n"] == 1 else CLEAN_DRAFT

    monkeypatch.setattr("src.core.tailoring_graph.generate_text", _stub)
    out = run_tailoring_graph("j1")
    assert out["draft_attempts"] == 2
    assert out["verification"]["passed"] is True
    assert out["draft"] == CLEAN_DRAFT
    assert out.get("error") is None
    assert out["selected_bullets"]["job1"][0] == "job1 bullet 0"


def test_graph_stops_after_max_attempts(monkeypatch):
    monkeypatch.setattr(
        "src.core.tailoring_graph.build_job_tailoring_prompt",
        lambda job_id: _bundle(),
    )
    monkeypatch.setattr(
        "src.core.tailoring_graph.generate_text",
        lambda prompt, **kwargs: BAD_DRAFT,
    )
    out = run_tailoring_graph("j1")
    assert out["draft_attempts"] == MAX_DRAFT_ATTEMPTS
    assert out["verification"]["passed"] is False
    assert out["verification"]["failures"]


def test_graph_draft_failure_sets_error(monkeypatch):
    monkeypatch.setattr(
        "src.core.tailoring_graph.build_job_tailoring_prompt",
        lambda job_id: _bundle(),
    )

    def _boom(prompt, **kwargs):
        raise RuntimeError("all providers down")

    monkeypatch.setattr("src.core.tailoring_graph.generate_text", _boom)
    out = run_tailoring_graph("j1")
    assert out["draft_attempts"] == 1
    assert out["error"] == "draft failed: all providers down"
    assert out["verification"]["passed"] is False


def test_graph_compiles():
    assert build_tailoring_graph() is not None
