"""Tests for the anti-fabrication guardrails: fidelity rules, real job slugs,
bullet banks built from the verified base resume, and plain-voice email rules."""
import json
from pathlib import Path

from src.synthesis import prompt_builder as pb

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def test_bullet_distribution_uses_generic_slot_keys():
    # Keys are generic slots; each slot's employer header comes from its
    # framework's resume_slot block (tested separately) — never invented.
    assert set(pb.BULLET_DISTRIBUTION) == {"job1", "job2", "job3", "job4"}


def test_fidelity_rules_cover_identity_facts():
    rules = pb.FIDELITY_RULES.lower()
    for must_have in [
        "sri omkar d",
        "resume_slot",
        "never substitute",
        "never invent",
        "traceable",
        "omit",
    ]:
        assert must_have in rules, f"missing: {must_have}"


def test_email_voice_rules_ban_ai_tells_and_invention():
    rules = pb.EMAIL_VOICE_RULES
    for banned in ["leveraged", "robust", "seamless", "rigorous", "cutting-edge"]:
        assert banned in rules
    assert "tokenization" in rules  # the invented-technique example guard
    assert "100%" in rules


def test_email_prompt_includes_voice_rules_and_framework_context():
    prompt = pb.build_whatsapp_outreach_prompt(
        company_name="Ask Consulting",
        job_title="Data Engineer",
        extracted_jd="Healthcare, HIPAA, PHI",
        helper_name="Helper",
        resume_url="https://example.com/resume",
        framework_context="optum: healthcare migration",
    )
    assert "optum: healthcare migration" in prompt
    assert "leveraged" in prompt  # the ban list is present in the prompt
    assert "VOICE AND HONESTY RULES" in prompt


def test_email_prompt_without_framework_still_has_voice_rules():
    prompt = pb.build_whatsapp_outreach_prompt(
        company_name="C",
        job_title="T",
        extracted_jd="JD",
        helper_name="H",
        resume_url="https://example.com/r",
    )
    assert "VOICE AND HONESTY RULES" in prompt
    assert "Selected Resume Framework" not in prompt


def test_framework_bullet_banks_come_from_base_resume():
    resume_text = (CONFIG_DIR / "resumes" / "resume_de.md").read_text(encoding="utf-8")
    for fw_id in ["blue_yonder", "accenture", "thomson_reuters"]:
        fw = json.loads((CONFIG_DIR / "frameworks" / f"{fw_id}.json").read_text(encoding="utf-8"))
        assert fw["bullet_bank"], f"{fw_id} has empty bank"
        for bullet in fw["bullet_bank"]:
            # every bank bullet text must appear verbatim in the base resume
            assert bullet["text"] in resume_text, (
                f"{fw_id}:{bullet['id']} not traceable to base resume"
            )
            # every bank bullet must carry match metadata (skills and/or signals)
            # so the rank step can match it; process bullets (mapping, docs)
            # legitimately have no tool tags and match via signals.
            assert bullet["skills"] or bullet["signals"], (
                f"{fw_id}:{bullet['id']} has no match metadata"
            )


def test_thomson_reuters_bank_has_ssis_not_fabrication():
    fw = json.loads((CONFIG_DIR / "frameworks" / "thomson_reuters.json").read_text(encoding="utf-8"))
    texts = " ".join(b["text"] for b in fw["bullet_bank"])
    assert "SSIS" in texts
    assert "Led the migration" not in texts
    assert "MapReduce" not in texts


def test_optum_bank_unchanged_and_verified():
    fw = json.loads((CONFIG_DIR / "frameworks" / "optum.json").read_text(encoding="utf-8"))
    assert len(fw["bullet_bank"]) == 14
    texts = " ".join(b["text"] for b in fw["bullet_bank"])
    assert "30+ legacy jobs" in texts
    assert "7:30" in texts
