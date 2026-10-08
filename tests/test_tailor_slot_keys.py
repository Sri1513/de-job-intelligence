"""Regression test: tailor must never pass the slot map's "job1_rationale"
entry to load_framework(). The rationale is a free-text sentence, not a
framework id; passing it produced "Error loading framework <sentence>"
on every WhatsApp alert run (live MCP logs, 2026-10-07)."""
from unittest.mock import patch

from src.engine import tailor as tailor_mod


def _arguments():
    return {
        "company_name": "Acme",
        "job_title": "Data Engineer",
        "tailored_content": {"summary": "x"},
        "slot_frameworks": {
            "job1": "herc_rentals",
            "job1_rationale": "The job posting lacks any healthcare-specific domain signals.",
            "job2": "blue_yonder",
            "job3": "accenture",
            "job4": "thomson_reuters",
        },
    }


def _fake_framework(fw_id):
    return {"resume_slot": {"header": f"{fw_id} header"}}


def test_rationale_key_never_reaches_load_framework():
    seen = []

    def fake_load(fw_id):
        seen.append(fw_id)
        return _fake_framework(fw_id)

    with (
        patch.object(tailor_mod, "load_framework", side_effect=fake_load),
        patch.object(
            tailor_mod,
            "generate_resume_from_llm_payload",
            return_value="https://docs.example/resume",
        ) as mock_gen,
    ):
        result = tailor_mod.export_tailored_resume_to_drive(_arguments())

    assert result["status"] == "success"
    assert seen == ["herc_rentals", "blue_yonder", "accenture", "thomson_reuters"]
    assert not any("healthcare" in str(fw_id) for fw_id in seen)
    slot_headers = mock_gen.call_args.kwargs["slot_headers"]
    assert set(slot_headers) == {"job1", "job2", "job3", "job4"}
    assert "job1_rationale" not in slot_headers


def test_missing_slot_keys_are_skipped():
    with (
        patch.object(tailor_mod, "load_framework", side_effect=_fake_framework) as mock_load,
        patch.object(
            tailor_mod,
            "generate_resume_from_llm_payload",
            return_value="https://docs.example/resume",
        ),
    ):
        result = tailor_mod.export_tailored_resume_to_drive(
            {
                "company_name": "Acme",
                "job_title": "Data Engineer",
                "tailored_content": {},
                "slot_frameworks": {"job1": "optum"},
            }
        )

    assert result["status"] == "success"
    assert [c.args[0] for c in mock_load.call_args_list] == ["optum"]
