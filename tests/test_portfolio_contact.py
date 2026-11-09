"""Tests for the portfolio appointment / notify flow (portfolio_contact).

Detection, extraction, save, WhatsApp ping, and the end-to-end
handle_submission orchestration. No live model, DB, or WhatsApp is
involved -- all three are faked.
"""
from contextlib import contextmanager

from src.core.config import settings
from src.dashboard import portfolio_contact

ASK_HISTORY = [
    {"role": "user", "text": "How do I contact him?"},
    {
        "role": "assistant",
        "text": (
            "Please share your name and the best way to reach you, "
            "plus your reason and a few times that work for you."
        ),
    },
]

DETAILS = {
    "name": "Priya",
    "contact": "priya@example.com",
    "reason": "Data Engineer role",
    "timings": "Tue-Thu afternoons",
}


def test_has_contact_info():
    assert portfolio_contact.has_contact_info("Reach me at priya@example.com")
    assert portfolio_contact.has_contact_info("Call me on +1 (951) 555-0134")
    assert not portfolio_contact.has_contact_info("I want an appointment")
    assert not portfolio_contact.has_contact_info("")


def test_asked_for_details():
    assert portfolio_contact.asked_for_details(ASK_HISTORY)
    assert not portfolio_contact.asked_for_details(
        [{"role": "assistant", "text": "Here is how to contact him."}]
    )
    assert not portfolio_contact.asked_for_details([])


def test_detect_submission():
    msg = "I'm Priya, priya@example.com, free Tue-Thu afternoons"
    assert portfolio_contact.detect_submission(ASK_HISTORY, msg)
    assert portfolio_contact.detect_submission(
        ASK_HISTORY, "Call me on +1 (951) 555-0134"
    )
    # No history, but the message itself carries intent + contact info.
    assert portfolio_contact.detect_submission(
        [], "I want an appointment, I'm Priya priya@example.com"
    )
    # Contact info with no intent and no prior ask is not a submission
    # (e.g. a visitor asking what Omkar's own email is).
    assert not portfolio_contact.detect_submission([], "What is his email address?")
    # Asked for details but the visitor shared no contact info yet.
    assert not portfolio_contact.detect_submission(ASK_HISTORY, "I'm Priya")
    assert not portfolio_contact.detect_submission(ASK_HISTORY, "yes")


def test_classify_kind():
    assert portfolio_contact.classify_kind("I'd like to meet him next week") == "appointment"
    assert (
        portfolio_contact.classify_kind("Just let him know I reached out")
        == "notify"
    )
    assert portfolio_contact.classify_kind("Here are my details") == "appointment"


def _fake_attempt(model, fn):
    return {
        "choices": [
            {"message": {"content": (
                '{"name": "Priya", "contact": "priya@example.com", '
                '"reason": "Data Engineer role", "timings": "Tue-Thu afternoons"}'
            )}}
        ]
    }


def test_extract_details_success(monkeypatch):
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat")
    monkeypatch.setattr(portfolio_contact, "attempt_with_failover", _fake_attempt)
    assert portfolio_contact.extract_details("I'm Priya, priya@example.com") == DETAILS


def test_extract_details_tolerates_code_fence(monkeypatch):
    def _fenced(model, fn):
        return {"choices": [{"message": {"content": "```json\n" + '{"name": "Priya", "contact": null, "reason": null, "timings": null}' + "\n```"}}]}

    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat")
    monkeypatch.setattr(portfolio_contact, "attempt_with_failover", _fenced)
    result = portfolio_contact.extract_details("I'm Priya")
    assert result["name"] == "Priya"
    assert result["contact"] is None


def test_extract_details_failures(monkeypatch):
    # No model URL configured.
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "")
    assert portfolio_contact.extract_details("I'm Priya, priya@example.com") is None
    # Non-JSON reply.
    monkeypatch.setattr(settings, "MARKET_CHAT_LLM_URL", "http://llm.test/api/chat")
    monkeypatch.setattr(
        portfolio_contact,
        "attempt_with_failover",
        lambda model, fn: {"choices": [{"message": {"content": "not json"}}]},
    )
    assert portfolio_contact.extract_details("I'm Priya, priya@example.com") is None
    # Endpoint raises.
    def _boom(model, fn):
        raise RuntimeError("down")

    monkeypatch.setattr(portfolio_contact, "attempt_with_failover", _boom)
    assert portfolio_contact.extract_details("I'm Priya, priya@example.com") is None


class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        self.store.setdefault("statements", []).append(sql)

    def fetchone(self):
        return {"id": 7}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, store):
        self.store = store
        self.cursor_obj = _FakeCursor(store)
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_db(monkeypatch, store):
    @contextmanager
    def _connect():
        yield _FakeConn(store)

    monkeypatch.setattr(portfolio_contact, "get_db_connection", _connect)
    return store


def test_save_request(monkeypatch):
    store = _fake_db(monkeypatch, {})
    request_id = portfolio_contact.save_request(DETAILS, "appointment", "raw message")
    assert request_id == 7
    assert store["statements"][0].startswith("INSERT INTO portfolio_contact_requests")
    # The cursor is reachable through a fresh fake: re-run with capture.
    captured = {}

    @contextmanager
    def _capturing():
        conn = _FakeConn(captured)
        yield conn
        captured["params"] = conn.cursor_obj.params
        captured["committed"] = conn.committed

    monkeypatch.setattr(portfolio_contact, "get_db_connection", _capturing)
    portfolio_contact.save_request(DETAILS, "notify", "raw message")
    assert captured["params"] == (
        "notify", "Priya", "priya@example.com",
        "Data Engineer role", "Tue-Thu afternoons", "raw message",
    )
    assert captured["committed"] is True


def test_notify_omkar(monkeypatch):
    sent = {}

    def _capture(msg):
        sent["msg"] = msg
        return True

    monkeypatch.setattr(portfolio_contact, "send_whatsapp", _capture)
    marked = {}
    monkeypatch.setattr(portfolio_contact, "_mark_notified", lambda rid: marked.setdefault("id", rid))
    assert portfolio_contact.notify_omkar(DETAILS, "appointment", 12, "raw") is True
    assert "appointment request #12" in sent["msg"]
    assert "Priya" in sent["msg"]
    assert "priya@example.com" in sent["msg"]
    assert marked["id"] == 12


def test_notify_omkar_failure_does_not_mark(monkeypatch):
    monkeypatch.setattr(portfolio_contact, "send_whatsapp", lambda msg: False)
    marked = []
    monkeypatch.setattr(portfolio_contact, "_mark_notified", lambda rid: marked.append(rid))
    assert portfolio_contact.notify_omkar(DETAILS, "notify", 12, "raw") is False
    assert marked == []


def test_confirmation_text():
    assert portfolio_contact.confirmation_text(DETAILS) == (
        "Thanks Priya, I've passed your details to Omkar -- "
        "he'll reach out at priya@example.com."
    )
    assert portfolio_contact.confirmation_text({"contact": "priya@example.com"}) == (
        "Thanks, I've passed your details to Omkar -- "
        "he'll reach out at priya@example.com."
    )
    assert portfolio_contact.confirmation_text({}) == (
        "Thanks, I've passed your details to Omkar."
    )


def test_handle_submission_happy_path(monkeypatch):
    monkeypatch.setattr(portfolio_contact, "extract_details", lambda msg: DETAILS)
    monkeypatch.setattr(portfolio_contact, "save_request", lambda d, k, raw: 12)
    monkeypatch.setattr(portfolio_contact, "notify_omkar", lambda d, k, rid, raw: True)
    reply = portfolio_contact.handle_submission("I'm Priya, priya@example.com", ASK_HISTORY)
    assert reply == portfolio_contact.confirmation_text(DETAILS)


def test_handle_submission_total_failure(monkeypatch):
    monkeypatch.setattr(portfolio_contact, "extract_details", lambda msg: None)
    monkeypatch.setattr(portfolio_contact, "save_request", lambda d, k, raw: None)
    monkeypatch.setattr(portfolio_contact, "notify_omkar", lambda d, k, rid, raw: False)
    reply = portfolio_contact.handle_submission("I'm Priya, priya@example.com", ASK_HISTORY)
    assert "couldn't pass that along" in reply


def test_ensure_contact_requests_table(monkeypatch):
    store = _fake_db(monkeypatch, {})
    portfolio_contact.ensure_contact_requests_table()
    assert any(
        "CREATE TABLE IF NOT EXISTS portfolio_contact_requests" in s
        for s in store["statements"]
    )
