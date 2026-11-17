"""Tailoring StateGraph — Sprint 4, Lesson 1.

The tailoring path used to be a hand-off: ``build_job_tailoring_prompt``
built the bundle, the caller drafted outside the server, and nothing
inside checked the draft. This module turns it into a pipeline that
runs inside the server — four nodes sharing one state, with a retry
arrow back from verify to draft:

    retrieve -> rank -> draft -> verify
                         ^           |
                         +-----------+  (only when verify fails and
                                         retries remain)

Nodes
-----
retrieve
    Runs ``build_job_tailoring_prompt`` (Sprint 3, Lessons 2+3) and
    puts the bundle on the state. A failure is recorded as ``error``
    instead of raising, so the graph ends cleanly.
rank
    Pure function, no LLM, no DB. Keeps the top 3 bullets per slot
    from the bundle's deterministic ranking — the draft's working set.
draft
    One ``generate_text`` call (free-only chain) over the job, the
    selected bullets, the acronym rule and the fidelity rules. On a
    retry it also sees the previous verification failures.
verify
    Pure checks, no LLM. Banned words, absolute claims, identity
    (candidate name present, no forbidden acronym expansion). This is
    the cheap-check layer — it does NOT prove bullet traceability to
    the bank; that is Lesson 2/3 eval territory (RAGAS faithfulness).

The graph is synchronous throughout (the builder and ``generate_text``
are both sync), so the MCP dispatcher can run it via
``asyncio.to_thread``, the same way it runs the builder today. No
dispatcher wiring in this lesson — the graph proves itself live
first.
"""

import logging
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from src.engine.llm_text import generate_text
from src.synthesis.prompt_builder import build_job_tailoring_prompt

logger = logging.getLogger(__name__)

# How many times the draft node may run for one job (first draft + 1 retry).
MAX_DRAFT_ATTEMPTS = 2
# Bullets kept per slot from the deterministic ranking.
TOP_BULLETS_PER_SLOT = 3

# Anti-fabrication contract: words that must never appear in a draft.
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
# Absolute claims that must never appear in a draft.
ABSOLUTE_PATTERNS = (
    r"100\s*%",
    r"0\s*%\s*data loss",
    r"zero\s+data loss",
    r"zero\s+downtime",
    r"guarantee",
)
CANDIDATE_NAME = "Sri Omkar D"
# Acronym expansions that are known-wrong (Lesson 3: the MCP hallucination).
FORBIDDEN_EXPANSIONS = ("Microsoft Certified Professional",)


class TailoringState(TypedDict, total=False):
    """The clipboard every node reads from and writes to."""

    job_id: str
    bundle: dict
    selected_bullets: dict
    draft: str
    draft_attempts: int
    verification: dict
    error: str | None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def retrieve_node(state: TailoringState) -> dict:
    """Build the tailoring bundle for the job (Sprint 3 builder)."""
    job_id = state.get("job_id", "")
    try:
        bundle = build_job_tailoring_prompt(job_id)
    except Exception as exc:  # noqa: BLE001 - recorded on state, graph ends
        logger.warning("tailoring graph: retrieve failed for %s: %s", job_id, exc)
        return {"error": f"retrieve failed: {exc}"}
    return {"bundle": bundle}


def rank_node(state: TailoringState) -> dict:
    """Keep the top N bullets per slot from the deterministic ranking."""
    if state.get("error") or "bundle" not in state:
        return {}
    ranking = (
        state["bundle"].get("tailoring_guidelines", {}).get("deterministic_bullet_ranking", {})
    )
    selected = {}
    for slot, bullets in ranking.items():
        selected[slot] = [b.get("text", "") for b in (bullets or [])[:TOP_BULLETS_PER_SLOT]]
    return {"selected_bullets": selected}


def _build_draft_prompt(state: TailoringState) -> str:
    """The only input the draft LLM sees: job + selected bullets + rules."""
    bundle = state.get("bundle", {})
    meta = bundle.get("job_metadata", {})
    guidelines = bundle.get("tailoring_guidelines", {})
    lines = [
        "Write the tailored resume content for this role.",
        f"Role: {meta.get('title', '')} at {meta.get('company', '')}.",
        f"Candidate: {CANDIDATE_NAME}.",
        "",
        "Use ONLY the selected experience bullets below, grouped by slot. "
        "Do not invent tools, employers, metrics, or responsibilities.",
    ]
    for slot, bullets in (state.get("selected_bullets") or {}).items():
        lines.append(f"\n[{slot}]")
        lines.extend(f"- {b}" for b in bullets)
    acronym_rule = guidelines.get("acronym_rule", "")
    if acronym_rule:
        lines.append(f"\n{acronym_rule}")
    lines.append(
        "\nWrite plainly. Never use: leveraging, spearheaded, mastered, "
        "passionate, cutting-edge, robust, seamless, rigorous, "
        "state-of-the-art. Never claim 100%, zero data loss, zero "
        "downtime, or any guarantee."
    )
    failures = (state.get("verification") or {}).get("failures") or []
    if failures:
        lines.append("\nThe previous draft FAILED verification:")
        lines.extend(f"- {f}" for f in failures)
        lines.append("Fix every failure above; keep everything else.")
    return "\n".join(lines)


def draft_node(state: TailoringState) -> dict:
    """One LLM call over the prompt built from the state."""
    if state.get("error") or "bundle" not in state:
        return {}
    attempts = state.get("draft_attempts", 0) + 1
    try:
        draft = generate_text(
            _build_draft_prompt(state),
            task="tailoring_draft",
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001 - recorded on state, graph ends
        logger.warning("tailoring graph: draft failed (attempt %d): %s", attempts, exc)
        return {"draft_attempts": attempts, "error": f"draft failed: {exc}"}
    return {"draft": draft, "draft_attempts": attempts}


def verify_node(state: TailoringState) -> dict:
    """Cheap fidelity checks on the draft. No LLM. Failures are named."""
    if state.get("error") or not state.get("draft"):
        return {"verification": {"passed": False, "failures": ["no draft to verify"]}}
    draft = state["draft"]
    lowered = draft.lower()
    failures = []
    for word in BANNED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            failures.append(f"banned word: {word}")
    for pattern in ABSOLUTE_PATTERNS:
        if re.search(pattern, lowered):
            failures.append(f"absolute claim: {pattern}")
    if CANDIDATE_NAME not in draft:
        failures.append(f"missing candidate name: {CANDIDATE_NAME}")
    for expansion in FORBIDDEN_EXPANSIONS:
        if expansion.lower() in lowered:
            failures.append(f"forbidden acronym expansion: {expansion}")
    return {"verification": {"passed": not failures, "failures": failures}}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route_after_retrieve(state: TailoringState) -> str:
    """A failed retrieve ends the graph; otherwise rank next."""
    return "done" if state.get("error") else "rank"


def route_after_verify(state: TailoringState) -> str:
    """Pass, or out of retries, or an error -> done. Else draft again."""
    if state.get("error"):
        return "done"
    if (state.get("verification") or {}).get("passed"):
        return "done"
    if state.get("draft_attempts", 0) >= MAX_DRAFT_ATTEMPTS:
        return "done"
    return "draft"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_tailoring_graph():
    """Compile the four-node graph. No side effects until invoked."""
    graph = StateGraph(TailoringState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("rank", rank_node)
    graph.add_node("draft", draft_node)
    graph.add_node("verify", verify_node)
    graph.add_edge(START, "retrieve")
    graph.add_conditional_edges("retrieve", route_after_retrieve, {"rank": "rank", "done": END})
    graph.add_edge("rank", "draft")
    graph.add_edge("draft", "verify")
    graph.add_conditional_edges("verify", route_after_verify, {"draft": "draft", "done": END})
    return graph.compile()


def run_tailoring_graph(job_id: str) -> dict[str, Any]:
    """Run the graph for one job and return the final state."""
    return build_tailoring_graph().invoke({"job_id": job_id})
