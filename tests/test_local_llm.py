# tests/test_local_llm.py
"""Unit tests for the local-LLM (qwen2.5:3b) fallback tier.

All model calls are stubbed (no live calls in unit tests): the transport
function src.engine.local_llm._chat_complete is patched per test.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from src.engine import local_llm
from src.engine.local_llm import (
    LocalLLMError,
    ai_notes,
    checklist_extract,
    extract_skills,
    normalize_extracted,
    sponsorship_check,
    union_checklists,
    verbatim_safety_net,
)

VOCAB = ["Python", "PySpark", "Apache Spark", "SQL", "audit", "API integration"]


# ---------------------------------------------------------------------------
# Verbatim safety net
# ---------------------------------------------------------------------------

def test_verbatim_net_word_boundaries():
    # "audit" matches "audit trails" but NOT "auditor".
    assert verbatim_safety_net("clean audit trails for every system", ["audit"]) == ["audit"]
    assert verbatim_safety_net("the external auditor reviewed us", ["audit"]) == []


def test_verbatim_net_case_insensitive_and_multiword():
    text = "Strong API Integration and PYTHON skills required."
    assert verbatim_safety_net(text, ["API integration", "Python"]) == [
        "API integration",
        "Python",
    ]


def test_verbatim_net_longest_first_no_double_count():
    # "PySpark" consumes the span so "Spark" (a shorter name) cannot
    # double-count the same text.
    text = "Built PySpark pipelines for the lakehouse."
    assert verbatim_safety_net(text, ["Spark", "PySpark"]) == ["PySpark"]
    # But a standalone "Spark" still matches when PySpark is absent.
    assert verbatim_safety_net("Built Spark pipelines.", ["Spark", "PySpark"]) == ["Spark"]


def test_verbatim_net_names_only_no_alias_false_positive():
    # The alias "spark" (verb) must NOT match "Apache Spark": the net is
    # names-only, so "your contributions spark real impact" yields nothing.
    text = "an environment where your contributions spark real impact"
    assert verbatim_safety_net(text, ["Apache Spark", "PySpark"]) == []


def test_verbatim_net_empty_jd():
    assert verbatim_safety_net("", VOCAB) == []
    assert verbatim_safety_net("   ", VOCAB) == []


# ---------------------------------------------------------------------------
# Union logic
# ---------------------------------------------------------------------------

def test_union_checklists_covers_drops():
    pass1 = {"Python": True, "SQL": False, "audit": True}
    pass2 = {"Python": False, "SQL": True, "audit": True}
    assert union_checklists(pass1, pass2) == ["Python", "SQL", "audit"]


def test_union_checklists_false_in_both_stays_out():
    assert union_checklists({"Python": False}, {"Python": False}) == []


def test_union_checklists_empty_pass():
    assert union_checklists({}, {"Python": True}) == ["Python"]
    assert union_checklists({}, {}) == []


# ---------------------------------------------------------------------------
# normalize_extracted (exact + alias only; fuzzy deliberately off)
# ---------------------------------------------------------------------------

def test_normalize_extracted_exact_and_alias():
    # "PySpark" exact; "py spark" is a taxonomy alias of PySpark.
    assert normalize_extracted(["PySpark", "py spark", "Python"]) == [
        "PySpark",
        "PySpark",
        "Python",
    ]


def test_normalize_extracted_unmatched_stays_free_text():
    assert normalize_extracted(["QuantumFluxDB", "Python"]) == ["QuantumFluxDB", "Python"]


def test_normalize_extracted_no_fuzzy_even_for_typos():
    # "Kubernates" would fuzzy-match "kubernetes" if rapidfuzz ran; the
    # queue's post-filter is exact + alias only until the VM rapidfuzz
    # install is verified, so a typo stays free text here.
    assert normalize_extracted(["Kubernates"]) == ["Kubernates"]


# ---------------------------------------------------------------------------
# Checklist extraction (stubbed transport)
# ---------------------------------------------------------------------------

def _checklist_reply(mapping):
    return json.dumps({"skills": mapping})


def test_checklist_extract_adaptive_chunk_size():
    # Short JD -> 64-key chunks (2 calls for 128 skills); long JD
    # (>4000 chars) -> 32-key chunks (4 calls). The battery showed
    # 64-key chunks 524 on the 7,752-char nSCALE JD even warm.
    vocab = [f"Skill{i}" for i in range(128)]
    short_jd = "short JD"
    long_jd = "x" * 5000

    def reply_for(chunk_prompt):
        # Return a valid all-false mapping for whatever keys the prompt lists.
        import re as _re
        keys = _re.findall(r'"(Skill\d+)"', chunk_prompt)
        return json.dumps({"skills": {k: False for k in keys}})

    with patch.object(local_llm, "_chat_complete", side_effect=reply_for) as mock_call:
        out = checklist_extract(short_jd, vocab)
    assert len(out) == 128
    assert mock_call.call_count == 2
    assert mock_call.call_args_list[0].args[0].count("Skill") >= 64

    with patch.object(local_llm, "_chat_complete", side_effect=reply_for) as mock_call:
        out = checklist_extract(long_jd, vocab)
    assert len(out) == 128
    assert mock_call.call_count == 4


def test_checklist_extract_explicit_chunk_size_honored():
    vocab = ["Python", "SQL", "audit"]
    with patch.object(
        local_llm, "_chat_complete",
        return_value=_checklist_reply({"Python": False, "SQL": False, "audit": False}),
    ) as mock_call:
        checklist_extract("x" * 5000, vocab, chunk_size=3)
    assert mock_call.call_count == 1  # explicit 3 beats the adaptive 32


def test_checklist_extract_strict_parse_all_keys():
    replies = [
        _checklist_reply({"Python": True, "SQL": False}),
        _checklist_reply({"Python": True, "SQL": True}),
    ]
    with patch.object(local_llm, "_chat_complete", side_effect=replies) as mock_call:
        out = checklist_extract("some JD", ["Python", "SQL"], chunk_size=2)
    # One chunk (2 skills, chunk_size 2) -> only the first reply is used.
    assert out == {"Python": True, "SQL": False}
    assert mock_call.call_count == 1


def test_checklist_extract_chunks_and_merges():
    # 3 skills, chunk_size 2 -> two calls, merged in order.
    replies = [
        _checklist_reply({"Python": True, "SQL": False}),
        _checklist_reply({"audit": True}),
    ]
    with patch.object(local_llm, "_chat_complete", side_effect=replies) as mock_call:
        out = checklist_extract("some JD", ["Python", "SQL", "audit"], chunk_size=2)
    assert out == {"Python": True, "SQL": False, "audit": True}
    assert mock_call.call_count == 2
    # Each chunk prompt lists only its own skills.
    assert '"Python"' in mock_call.call_args_list[0].args[0]
    assert '"audit"' not in mock_call.call_args_list[0].args[0]
    assert '"audit"' in mock_call.call_args_list[1].args[0]


def test_checklist_extract_few_missing_keys_treated_as_false():
    # Battery finding: the 3B drops ~1 key per 64-key JSON (AS400 3x,
    # PHI Masking 1x); the dropped keys were genuinely false. Up to
    # MAX_MISSING_CHECKLIST_KEYS missing keys are recorded as false.
    partial = _checklist_reply({"Python": True})  # SQL missing
    with patch.object(local_llm, "_chat_complete", return_value=partial) as mock_call:
        out = checklist_extract("some JD", ["Python", "SQL"], chunk_size=2)
    assert out == {"Python": True, "SQL": False}
    assert mock_call.call_count == 1  # no retry needed


def test_checklist_extract_many_missing_keys_retries_once_then_fails():
    # More than MAX_MISSING_CHECKLIST_KEYS missing is a genuinely broken
    # response: retry once, then fail.
    bad = _checklist_reply({"Python": True})  # 4 of 5 keys missing
    vocab = ["Python", "SQL", "audit", "Kafka", "Airflow"]
    with patch.object(local_llm, "_chat_complete", side_effect=[bad, bad]) as mock_call:
        with pytest.raises(LocalLLMError, match="missing keys"):
            checklist_extract("some JD", vocab, chunk_size=5)
    assert mock_call.call_count == 2
    assert "Reply with JSON only" in mock_call.call_args_list[1].args[0]


def test_checklist_extract_extra_key_recovered_on_retry():
    bad = _checklist_reply({"Python": True, "SQL": False, "Cobol": True})
    good = _checklist_reply({"Python": True, "SQL": False})
    with patch.object(local_llm, "_chat_complete", side_effect=[bad, good]) as mock_call:
        out = checklist_extract("some JD", ["Python", "SQL"], chunk_size=2)
    assert out == {"Python": True, "SQL": False}
    assert mock_call.call_count == 2


def test_checklist_extract_extra_key_is_rejected():
    bad = _checklist_reply({"Python": True, "SQL": False, "Cobol": True})
    with patch.object(local_llm, "_chat_complete", side_effect=[bad, bad]):
        with pytest.raises(LocalLLMError, match="outside the list"):
            checklist_extract("some JD", ["Python", "SQL"], chunk_size=2)


def test_checklist_extract_non_json_fails():
    with patch.object(local_llm, "_chat_complete", side_effect=["not json at all"] * 2):
        with pytest.raises(LocalLLMError, match="not JSON"):
            checklist_extract("some JD", ["Python"], chunk_size=1)


def test_checklist_extract_transport_failure_retried_once():
    # A cold-start 524 on the first attempt is retried; the warm retry
    # succeeds, so the chunk returns instead of failing the job.
    good = _checklist_reply({"Python": True, "SQL": False})
    with patch.object(
        local_llm, "_chat_complete",
        side_effect=[local_llm.LocalLLMUnavailableError("HTTP 524"), good],
    ) as mock_call:
        out = checklist_extract("some JD", ["Python", "SQL"], chunk_size=2)
    assert out == {"Python": True, "SQL": False}
    assert mock_call.call_count == 2


def test_checklist_extract_transport_failure_twice_stays_tier_down():
    # Two transport failures: the tier-down error propagates (not a
    # per-job data failure), so the queue stops the batch instead of
    # marking the row FAILED.
    with patch.object(
        local_llm, "_chat_complete",
        side_effect=local_llm.LocalLLMUnavailableError("HTTP 524"),
    ):
        with pytest.raises(local_llm.LocalLLMUnavailableError, match="HTTP 524"):
            checklist_extract("some JD", ["Python", "SQL"], chunk_size=2)


def test_checklist_extract_markdown_fences_stripped():
    fenced = "```json\n" + _checklist_reply({"Python": True}) + "\n```"
    with patch.object(local_llm, "_chat_complete", return_value=fenced):
        assert checklist_extract("some JD", ["Python"], chunk_size=1) == {"Python": True}


def test_checklist_extract_prompt_lists_every_skill_in_order():
    captured = {}

    def fake_call(prompt, timeout=150):
        captured["prompt"] = prompt
        return _checklist_reply({"Python": False, "SQL": False, "audit": False})

    with patch.object(local_llm, "_chat_complete", side_effect=fake_call):
        checklist_extract("some JD", ["Python", "SQL", "audit"], chunk_size=3)
    prompt = captured["prompt"]
    assert prompt.index('"Python"') < prompt.index('"SQL"') < prompt.index('"audit"')
    assert "If unsure, answer false" in prompt


def test_extract_skills_full_pattern_unions_passes_and_net():
    # Pass 1 drops SQL, pass 2 drops Python; the verbatim net catches
    # "audit" (in the JD text) even though both passes say false.
    jd = "Python and audit trails. SQL is not mentioned here at all."
    replies = [
        _checklist_reply({"Python": True, "SQL": False, "audit": False}),
        _checklist_reply({"Python": False, "SQL": True, "audit": False}),
    ]
    with patch.object(local_llm, "_chat_complete", side_effect=replies):
        out = extract_skills(jd, ["Python", "SQL", "audit"])
    # union {Python, SQL} + net {audit} -> all three, normalized.
    assert out == ["Python", "SQL", "audit"]


# ---------------------------------------------------------------------------
# Sponsorship detection
# ---------------------------------------------------------------------------

def test_sponsorship_regex_matches_keywords():
    for sentence in [
        "We do not sponsor visas for this role.",
        "Candidates must be authorized to work in the US.",
        "H-1B sponsorship is available.",
        "This role requires a green card.",
        "OPT EAD holders are welcome to apply.",
        "STEM OPT candidates preferred.",
    ]:
        result = sponsorship_check(f"About the role. {sentence} More text.")
        assert result["mentioned"] is True, sentence
        assert result["quote"] == sentence


def test_sponsorship_regex_word_boundary_opt():
    # "OPT" must not match "option"/"optimal".
    with patch.object(local_llm, "_chat_complete") as mock_call:
        mock_call.return_value = json.dumps({"mentioned": False, "quote": None})
        result = sponsorship_check("We offer optional remote work and optimal hours.")
    assert result == {"mentioned": False, "sponsorship": "Not Mentioned", "quote": None}
    mock_call.assert_called_once()  # regex found nothing -> confirmation call ran


def test_sponsorship_equal_opportunities_trap():
    # The nSCALE trap: a diversity statement is NOT sponsorship.
    jd = (
        "We strongly encourage applications from people of colour, the "
        "LGBTQ+ community, people with disabilities, and veterans. "
        "Build privileged access workflows."
    )
    with patch.object(local_llm, "_chat_complete") as mock_call:
        mock_call.return_value = json.dumps({"mentioned": False, "quote": None})
        result = sponsorship_check(jd)
    assert result["mentioned"] is False
    assert result["sponsorship"] == "Not Mentioned"
    assert result["quote"] is None


def test_sponsorship_denial_sentence_means_no_sponsorship():
    jd = (
        "Candidates must be legally authorized to work in the United States. "
        "LendingTree will not provide or assume sponsorship for employment "
        "visas for this position, now or in the future."
    )
    result = sponsorship_check(jd)
    assert result["mentioned"] is True
    assert result["sponsorship"] == "No Sponsorship"
    assert "will not provide or assume sponsorship" in result["quote"]


def test_sponsorship_positive_mention_means_available():
    result = sponsorship_check("Visa sponsorship is available for this role.")
    assert result == {
        "mentioned": True,
        "sponsorship": "Available",
        "quote": "Visa sponsorship is available for this role.",
    }


def test_sponsorship_confirmation_when_regex_finds_nothing():
    jd = "Build data pipelines with Python and SQL. Strong team player."
    with patch.object(local_llm, "_chat_complete") as mock_call:
        mock_call.return_value = json.dumps({"mentioned": False, "quote": None})
        result = sponsorship_check(jd)
    assert result == {"mentioned": False, "sponsorship": "Not Mentioned", "quote": None}
    prompt = mock_call.call_args.args[0]
    assert "equal-opportunities" in prompt


def test_sponsorship_confirmation_invented_quote_is_rejected():
    jd = "Build data pipelines with Python and SQL."
    bad = json.dumps({"mentioned": True, "quote": "We sponsor all visas."})
    good = json.dumps({"mentioned": False, "quote": None})
    with patch.object(local_llm, "_chat_complete", side_effect=[bad, good]):
        result = sponsorship_check(jd)
    assert result["mentioned"] is False


# ---------------------------------------------------------------------------
# AI Notes
# ---------------------------------------------------------------------------

def test_ai_notes_returns_plain_text():
    with patch.object(local_llm, "_chat_complete", return_value="Senior DE role. 7+ years with Spark.") as mock_call:
        assert ai_notes("some JD") == "Senior DE role. 7+ years with Spark."
    assert "2-3 sentences" in mock_call.call_args.args[0]


def test_sponsorship_confirmation_transport_failure_retried_once():
    jd = "Build data pipelines with Python and SQL."
    good = json.dumps({"mentioned": False, "quote": None})
    with patch.object(
        local_llm, "_chat_complete",
        side_effect=[local_llm.LocalLLMUnavailableError("HTTP 524"), good],
    ) as mock_call:
        result = sponsorship_check(jd)
    assert result == {"mentioned": False, "sponsorship": "Not Mentioned", "quote": None}
    assert mock_call.call_count == 2


def test_sponsorship_confirmation_transport_failure_twice_stays_tier_down():
    jd = "Build data pipelines with Python and SQL."
    with patch.object(
        local_llm, "_chat_complete",
        side_effect=local_llm.LocalLLMUnavailableError("HTTP 524"),
    ):
        with pytest.raises(local_llm.LocalLLMUnavailableError, match="HTTP 524"):
            sponsorship_check(jd)


def test_ai_notes_transport_failure_retried_once():
    with patch.object(
        local_llm, "_chat_complete",
        side_effect=[local_llm.LocalLLMUnavailableError("HTTP 524"), "Senior DE role."],
    ) as mock_call:
        assert ai_notes("some JD") == "Senior DE role."
    assert mock_call.call_count == 2


def test_ai_notes_transport_failure_twice_stays_tier_down():
    with patch.object(
        local_llm, "_chat_complete",
        side_effect=local_llm.LocalLLMUnavailableError("HTTP 524"),
    ):
        with pytest.raises(local_llm.LocalLLMUnavailableError, match="HTTP 524"):
            ai_notes("some JD")


def test_ai_notes_empty_reply_fails():
    with patch.object(local_llm, "_chat_complete", return_value="   "):
        with pytest.raises(LocalLLMError, match="empty"):
            ai_notes("some JD")


# ---------------------------------------------------------------------------
# Transport (stubbed urlopen)
# ---------------------------------------------------------------------------

def _fake_response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_chat_complete_sends_browser_ua_and_bearer_and_parses_openai_shape(monkeypatch):
    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_URL", "https://example.test/api/chat/completions")
    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_KEY", "secret-key")
    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_MODEL", "qwen2.5:3b")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _fake_response({"choices": [{"message": {"content": "hello"}}]})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert local_llm._chat_complete("hi") == "hello"
    assert "Mozilla/5.0" in captured["headers"]["User-agent"]
    assert "Chrome" in captured["headers"]["User-agent"]
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["timeout"] == 150
    assert captured["body"]["model"] == "qwen2.5:3b"
    assert captured["body"]["temperature"] == 0


def test_chat_complete_parses_ollama_native_shape(monkeypatch):
    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_URL", "https://example.test/api/chat/completions")
    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_KEY", "k")
    with patch("urllib.request.urlopen", return_value=_fake_response({"message": {"content": "native"}})):
        assert local_llm._chat_complete("hi") == "native"


def test_chat_complete_http_error_is_unavailable(monkeypatch):
    import urllib.error

    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_URL", "https://example.test/api/chat/completions")
    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_KEY", "k")
    err = urllib.error.HTTPError("https://example.test/", 403, "Forbidden", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(local_llm.LocalLLMUnavailableError, match="HTTP 403"):
            local_llm._chat_complete("hi")


def test_chat_complete_missing_config_is_unavailable(monkeypatch):
    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_URL", "")
    monkeypatch.setattr(local_llm.settings, "MARKET_CHAT_LLM_KEY", "")
    with pytest.raises(local_llm.LocalLLMUnavailableError, match="not configured"):
        local_llm._chat_complete("hi")
