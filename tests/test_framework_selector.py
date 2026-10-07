"""Tests for the shared LLM-judged framework selector."""
import json
from unittest.mock import patch

from src.engine import framework_selector as fs


def _llm_returning(payload):
    def _fake(prompt, **kwargs):
        assert kwargs.get("json_mode") is True
        return json.dumps(payload)

    return _fake


def test_registry_loads_all_frameworks():
    registry = fs.load_registry()
    ids = {k for k in registry if k not in ("default_framework", "slots")}
    assert ids == {"herc_rentals", "blue_yonder", "accenture", "thomson_reuters", "optum"}
    assert registry["default_framework"] == "herc_rentals"
    assert registry["slots"]["job1"]["healthcare"] == "optum"
    assert registry["slots"]["job1"]["default"] == "herc_rentals"


def test_optum_framework_has_bullet_bank():
    fw = fs.load_framework("optum")
    assert len(fw["bullet_bank"]) == 14
    assert all(b["text"] and b["skills"] for b in fw["bullet_bank"])


def test_thin_input_skips_llm_and_returns_default():
    with patch.object(fs, "generate_text") as mock_llm:
        result = fs.select_framework("Hi", "Data Engineer")
    mock_llm.assert_not_called()
    assert result["framework_id"] == "herc_rentals"
    assert result["thin"] is True


def test_healthcare_jd_selects_optum():
    jd = (
        "Senior ETL Engineer for a healthcare payer. Must have DataStage and SSIS "
        "experience migrating legacy jobs to AWS. HIPAA compliance and PHI masking required."
    )
    with patch.object(
        fs, "generate_text", _llm_returning({"framework": "optum", "rationale": "healthcare migration"})
    ):
        result = fs.select_framework(jd, "Senior ETL Engineer")
    assert result["framework_id"] == "optum"
    assert result["thin"] is False


def test_non_healthcare_jd_selects_herc_rentals():
    jd = (
        "Data Engineer needed for telematics platform. PySpark, Airflow, and Kinesis "
        "experience required for IoT data pipelines. " * 5
    )
    with patch.object(
        fs, "generate_text", _llm_returning({"framework": "herc_rentals", "rationale": "not healthcare"})
    ):
        result = fs.select_framework(jd, "Data Engineer")
    assert result["framework_id"] == "herc_rentals"


def test_unexpected_answer_falls_back_to_herc_rentals():
    with patch.object(
        fs, "generate_text", _llm_returning({"framework": "blue_yonder", "rationale": "oops"})
    ):
        result = fs.select_framework("Some data engineer job with SQL and Python." * 10)
    assert result["framework_id"] == "herc_rentals"


def test_llm_failure_falls_back_to_default():
    def _boom(prompt, **kwargs):
        raise RuntimeError("chain exhausted")

    with patch.object(fs, "generate_text", _boom):
        result = fs.select_framework("Some data engineer job with SQL and Python." * 10)
    assert result["framework_id"] == "herc_rentals"


def test_select_framework_never_raises():
    # Even garbage input must produce a valid selection dict.
    with patch.object(fs, "generate_text", side_effect=Exception("nope")):
        result = fs.select_framework("", "")
    assert set(result) == {"framework_id", "rationale", "thin"}


def test_select_slot_frameworks_healthcare():
    jd = "Healthcare Data Engineer, HIPAA, PHI, clinical data. " * 10
    with patch.object(
        fs, "generate_text", _llm_returning({"framework": "optum", "rationale": "healthcare"})
    ):
        slots = fs.select_slot_frameworks(jd, "Data Engineer")
    assert slots["job1"] == "optum"
    assert slots["job2"] == "blue_yonder"
    assert slots["job3"] == "accenture"
    assert slots["job4"] == "thomson_reuters"


def test_select_slot_frameworks_normal():
    jd = "Data Engineer, PySpark, Airflow, Snowflake. " * 10
    with patch.object(
        fs, "generate_text", _llm_returning({"framework": "herc_rentals", "rationale": "not healthcare"})
    ):
        slots = fs.select_slot_frameworks(jd, "Data Engineer")
    assert slots["job1"] == "herc_rentals"
    assert slots["job2"] == "blue_yonder"
    assert slots["job3"] == "accenture"
    assert slots["job4"] == "thomson_reuters"


def test_slot_frameworks_have_resume_slot_headers():
    for fw_id in ["optum", "herc_rentals", "blue_yonder", "accenture", "thomson_reuters"]:
        fw = fs.load_framework(fw_id)
        slot = fw.get("resume_slot", {})
        assert slot.get("header"), f"{fw_id} missing resume_slot.header"
        assert slot.get("dates"), f"{fw_id} missing resume_slot.dates"
