"""Lesson 3: company context + acronym glossary for the tailoring prompt.

Two pieces of verified knowledge that never reached the prompt:

1. Company context — config/frameworks/company_frameworks.json holds
   one entry per past employer (domain, business_context, tech_stack,
   metrics_and_impact). load_company_frameworks() reads it, but nothing
   in production called it. The selector below shapes one entry per
   resume slot, WITHOUT the process_framework: the prompt already gets
   each slot's phases through its per-slot framework file, so copying
   them again would only re-inflate the 27k-char dump Lesson 2 measured.

2. Acronym glossary — there is no glossary file anywhere in config/:
   the only MCP expansion in the codebase is the hallucination itself.
   The entries here are facts proven by a real misread (2026-11-11:
   the local 3B model flagged a JD as requiring "Microsoft Certified
   Professional"; the raw JD + trace showed MCP servers, Model Context
   Protocol). Add an entry ONLY for an acronym that has actually been
   misread — never pre-emptively.
"""

from __future__ import annotations

from typing import Any

# Verified acronym expansions. Key: the acronym as it appears in a JD.
# Value: the expansion the LLM must use. One entry per proven misread.
GLOSSARY: dict[str, str] = {
    "MCP": (
        "Model Context Protocol (Anthropic, late 2024) — the protocol for "
        "connecting AI agents to data/tools via MCP servers. NEVER expand "
        "as Microsoft Certified Professional."
    ),
}


def build_acronym_rule(glossary: dict[str, str] | None = None) -> str:
    """One mandatory rule block from the glossary; "" when it is empty."""
    entries = glossary if glossary is not None else GLOSSARY
    if not entries:
        return ""
    lines = ["ACRONYM RULE (MANDATORY — resolve acronyms FIRST, before reading the JD):"]
    lines += [f"- {acronym} = {expansion}" for acronym, expansion in entries.items()]
    return "\n".join(lines)


def _shape_entry(entry: dict[str, Any]) -> dict[str, Any]:
    # Only the fields the prompt does NOT already carry. process_framework
    # is deliberately left out — each slot's per-slot framework file
    # already supplies its phases to the prompt.
    return {
        "company_name": entry.get("company_name", ""),
        "domain": entry.get("domain", ""),
        "business_context": entry.get("business_context", ""),
        "tech_stack": list(entry.get("tech_stack") or []),
        "metrics_and_impact": dict(entry.get("metrics_and_impact") or {}),
    }


def select_company_context(
    slot_frameworks: dict[str, str],
    frameworks_data: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Shaped company context per resume slot.

    slot_frameworks maps slot -> framework id, e.g. {"job1": "optum",
    "job2": "blue_yonder", ...}. frameworks_data is the loaded
    company_frameworks.json (its "companies" mapping); when omitted it
    is loaded via load_company_frameworks(). A slot whose company has
    no entry — optum today, the file holds only four companies — maps
    to None, never to an invented entry.
    """
    # Local import, on purpose: prompt_builder will import this module
    # (Step 3), so a top-level import back would be a cycle. The same
    # pattern prompt_builder already uses for bullet_ranker.
    from src.synthesis.prompt_builder import load_company_frameworks

    if frameworks_data is None:
        frameworks_data = load_company_frameworks()
    companies = (frameworks_data or {}).get("companies") or {}
    return {
        slot: _shape_entry(companies[fw_id]) if fw_id in companies else None
        for slot, fw_id in slot_frameworks.items()
    }
