# src/dashboard/portfolio_contact.py
"""Appointment / notify flow for the portfolio drawer.

When a visitor asks how to contact Sri Omkar, the drawer offers to set
up an appointment or pass their details along. If the visitor says yes
and shares their name + a way to reach them, this module:

  1. detects the submission -- the conversation history shows the drawer
     asked for details and the visitor's message carries contact info,
     or the message itself carries both contact info and appointment
     intent;
  2. extracts the fields with one small model call (JSON only);
  3. saves the request to portfolio_contact_requests;
  4. pings Omkar on WhatsApp so he hears about it right away.

The drawer is stateless, so detection rides on the conversation history
the frontend posts with each message (last 6 turns). Nothing here books
a specific time or promises a reply by a date -- the confirmation says
only that the details were passed to Omkar. If the save or the ping
fails, the other channel is still attempted, and the visitor only hears
"passed along" when at least one of them succeeded.
"""
import json
import logging
import re

from src.core.config import settings
from src.core.database import get_db_connection
from src.core.llm_routing import attempt_with_failover
from src.dashboard.market_chat import _post_json
from src.engine.notifier import send_whatsapp

logger = logging.getLogger("de-job-intelligence.portfolio-contact")

MAX_HISTORY_TURNS = 6
MAX_HISTORY_CHARS = 500

# The model is instructed to ask for details with this exact phrase, so
# the detector can recognise that the previous assistant turn made the
# ask. Keep in sync with the portfolio_chat guardrails prompt.
ASK_DETAILS_PHRASE = "share your name"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?[\d\s().-]{7,}\d")

_APPOINTMENT_WORDS = (
    "appointment", "appointments", "meet", "meeting", "schedule",
    "book", "booking", "call", "talk", "video call",
)
_NOTIFY_WORDS = (
    "inform", "let him know", "reach out", "reaching out",
    "pass my", "pass along", "get in touch", "contact me",
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS portfolio_contact_requests (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind TEXT NOT NULL DEFAULT 'appointment',
    name TEXT,
    contact_info TEXT,
    reason TEXT,
    timings TEXT,
    raw_message TEXT NOT NULL,
    notified BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'new'
);
"""


def has_contact_info(text: str) -> bool:
    """True when the text carries an email address or a phone number."""
    if not text:
        return False
    if EMAIL_RE.search(text):
        return True
    match = PHONE_RE.search(text)
    if match and sum(ch.isdigit() for ch in match.group(0)) >= 7:
        return True
    return False


def _history_text(history) -> str:
    if not history:
        return ""
    parts = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        if isinstance(turn, dict):
            parts.append(str(turn.get("text") or ""))
    return " ".join(parts)


def asked_for_details(history) -> bool:
    """True when the last assistant turn asked the visitor for details."""
    if not history:
        return False
    for turn in reversed(history[-MAX_HISTORY_TURNS:]):
        if isinstance(turn, dict) and turn.get("role") == "assistant":
            return ASK_DETAILS_PHRASE in str(turn.get("text") or "").lower()
    return False


def has_appointment_intent(message: str, history=None) -> bool:
    """True when the message (or the recent conversation) carries
    appointment/notify intent -- 'yes' to the drawer's offer, a request
    to meet, or a request to be put in touch."""
    haystack = f"{message or ''} {_history_text(history)}".lower()
    return any(word in haystack for word in _APPOINTMENT_WORDS + _NOTIFY_WORDS)


def detect_submission(history, message: str) -> bool:
    """True when this message is a visitor handing over their details
    for an appointment or a notify: it carries contact info AND either
    the drawer just asked for details or the conversation shows
    appointment/notify intent."""
    if not has_contact_info(message):
        return False
    return asked_for_details(history) or has_appointment_intent(message, history)


def classify_kind(message: str, history=None) -> str:
    """'appointment' when the visitor wants to meet, 'notify' when they
    only want Omkar to know they reached out. Defaults to appointment:
    the drawer's offer lists it first."""
    haystack = f"{message or ''} {_history_text(history)}".lower()
    if any(word in haystack for word in _APPOINTMENT_WORDS):
        return "appointment"
    if any(word in haystack for word in _NOTIFY_WORDS):
        return "notify"
    return "appointment"


def extract_details(message: str):
    """One small model call: pull name, contact, reason, timings out of
    the visitor's message as JSON. Returns a dict with those keys (None
    for anything unstated), or None when the call fails or the reply
    is not usable JSON. The raw message is always saved alongside, so
    a failed extraction never loses the visitor's details."""
    url = settings.MARKET_CHAT_LLM_URL
    if not url:
        return None
    model = settings.MARKET_CHAT_LLM_MODEL
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract the visitor's contact details from the message. "
                    "Reply with ONLY a JSON object, no other text: "
                    '{"name": ..., "contact": ..., "reason": ..., "timings": ...}. '
                    "Use null for anything the message does not state. "
                    '"contact" is the visitor\'s email address or phone number.'
                ),
            },
            {"role": "user", "content": f"Message: {message}"},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 200,
    }
    try:
        response = attempt_with_failover(
            model,
            lambda ep: _post_json(
                ep.url, {**payload, "model": ep.model}, 60, ep.auth_headers
            ),
        )
    except Exception as exc:
        logger.warning("Portfolio-contact extraction failed: %s", exc)
        return None
    choices = response.get("choices") or []
    if choices:
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
    else:
        text = ((response.get("message") or {}).get("content") or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        logger.warning("Portfolio-contact extraction returned non-JSON: %r", text[:200])
        return None
    if not isinstance(parsed, dict):
        return None
    return {
        "name": _clean(parsed.get("name")),
        "contact": _clean(parsed.get("contact")),
        "reason": _clean(parsed.get("reason")),
        "timings": _clean(parsed.get("timings")),
    }


def _clean(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def ensure_contact_requests_table() -> None:
    """Best-effort CREATE TABLE IF NOT EXISTS at dashboard startup
    (CI deploy does not run migrations; migrations/006 records the
    same DDL for the manual path). Never blocks startup."""
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            conn.commit()
    except Exception as exc:
        logger.warning("Portfolio contact-requests table not created (serving without it): %s", exc)


def save_request(details, kind: str, raw_message: str):
    """Insert one contact request. Returns the new row id, or None when
    the insert fails (the caller still tries the WhatsApp ping)."""
    details = details or {}
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_contact_requests "
                "(kind, name, contact_info, reason, timings, raw_message) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
                (
                    kind,
                    details.get("name"),
                    details.get("contact"),
                    details.get("reason"),
                    details.get("timings"),
                    raw_message,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row["id"] if isinstance(row, dict) else row[0]
    except Exception as exc:
        logger.warning("Portfolio contact-request save failed: %s", exc)
        return None


def _mark_notified(request_id) -> None:
    if not request_id:
        return
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE portfolio_contact_requests SET notified = TRUE WHERE id = %s;",
                (request_id,),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Portfolio contact-request notified flag failed: %s", exc)


def notify_omkar(details, kind: str, request_id, raw_message: str) -> bool:
    """WhatsApp ping to Omkar with the request. Returns True on success.
    Falls back to the raw message when extraction failed, so he still
    gets the visitor's actual words."""
    details = details or {}
    label = "appointment request" if kind == "appointment" else "contact request"
    lines = [f"📅 New portfolio {label} #{request_id or '?'}:"]
    if details.get("name"):
        lines.append(f"From: {details['name']}")
    if details.get("contact"):
        lines.append(f"Reach them: {details['contact']}")
    if details.get("reason"):
        lines.append(f"Reason: {details['reason']}")
    if details.get("timings"):
        lines.append(f"Timings: {details['timings']}")
    if not details.get("name") and not details.get("contact"):
        lines.append(f"Details: {raw_message[:500]}")
    sent = send_whatsapp("\n".join(lines))
    if sent:
        _mark_notified(request_id)
    return sent


def confirmation_text(details) -> str:
    """What the visitor hears after a successful handoff. Never promises
    a time or a reply date -- only that the details reached Omkar."""
    details = details or {}
    name = details.get("name")
    contact = details.get("contact")
    if name and contact:
        return (
            f"Thanks {name}, I've passed your details to Omkar -- "
            f"he'll reach out at {contact}."
        )
    if contact:
        return (
            "Thanks, I've passed your details to Omkar -- "
            f"he'll reach out at {contact}."
        )
    return "Thanks, I've passed your details to Omkar."


def handle_submission(message: str, history=None) -> str:
    """End-to-end submission handling: extract, classify, save, ping.
    Returns the reply text for the visitor. The save and the ping are
    attempted independently; the visitor hears the confirmation when
    at least one succeeded."""
    details = extract_details(message)
    kind = classify_kind(message, history)
    request_id = save_request(details, kind, message)
    notified = notify_omkar(details, kind, request_id, message)
    if request_id or notified:
        return confirmation_text(details)
    return (
        "Sorry, I couldn't pass that along just now. Please reach Omkar "
        "directly using the contact methods above and he'll get back to you."
    )
