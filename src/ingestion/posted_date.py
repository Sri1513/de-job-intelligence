# src/ingestion/posted_date.py
"""Posted-date normalization for the 7-day deadline system.

One shared parser for every ingestion path (JobSpy, Dice, LinkedIn email
alerts, agent-staged jobs): turn whatever a source calls the posted date
into a plain calendar ``date``, or None when the source carries no usable
posted date.

Rules (owner-decided 2026-11-22, non-negotiable):
- The saved/ingested date (created_at / saved_at) is NEVER a proxy for the
  posted date. A source with no posted date yields NULL, full stop.
- Relative dates ("2 days ago") resolve against a reference date — the
  email's received date for alerts, today otherwise. Months are 30 days
  (documented approximation; postings only ever say "N months ago").
- Anything unparseable yields None (fail-open): a wrong posted date would
  silently mis-set a deadline, a missing one just opts the row out.
"""
import re
from datetime import date, datetime, timedelta

_SENTINELS = {"", "n/a", "na", "unknown", "recently", "none", "null"}

_RELATIVE_RE = re.compile(
    r"(?:posted\s+)?"
    r"(?P<num>\d+)\s*"
    r"(?P<unit>hours?|hrs?|days?|weeks?|months?|[hdw])"
    r"\s*ago",
    re.IGNORECASE,
)
_BARE_AGO_RE = re.compile(r"(?:posted\s+)?(?P<num>\d+)\s*d\s*ago", re.IGNORECASE)

# Absolute formats beyond ISO (JobSpy/Dice already emit ISO).
_ABSOLUTE_FORMATS = ("%Y/%m/%d", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y")


def _parse_absolute(text: str) -> date | None:
    """ISO first, then a few common posting-site formats."""
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in _ABSOLUTE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_relative(text: str, reference: date) -> date | None:
    """'2 days ago' / '1 week ago' / 'today' / 'yesterday' -> date."""
    lowered = text.strip().lower()
    if lowered in ("today", "just now", "just posted", "now"):
        return reference
    if lowered == "yesterday":
        return reference - timedelta(days=1)
    match = _RELATIVE_RE.search(text) or _BARE_AGO_RE.search(text)
    if not match:
        return None
    num = int(match.group("num"))
    unit = match.group("unit").lower()
    if unit.startswith("h"):
        return reference
    if unit.startswith("d"):
        return reference - timedelta(days=num)
    if unit.startswith("w"):
        return reference - timedelta(weeks=num)
    if unit.startswith("m"):
        return reference - timedelta(days=30 * num)
    return None


def parse_posted_date(value, reference: date | None = None) -> date | None:
    """Normalize any source's posted-date value to a calendar date.

    Accepts date/datetime objects (JobSpy, pandas Timestamps), ISO strings
    (Dice), and relative strings ("2 days ago", LinkedIn alerts). Returns
    None for sentinels ("N/A", "Unknown", "Recently"), missing values, and
    anything unparseable. Never falls back to today or the ingest date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in _SENTINELS:
        return None
    ref = reference or date.today()
    relative = _parse_relative(text, ref)
    if relative is not None:
        return relative
    return _parse_absolute(text)


def extract_posted_date_from_text(text: str, reference: date | None = None) -> date | None:
    """Find a posted-date mention inside a free-text line, or None.

    Used by the email-alert parser, whose chunks sometimes carry a line
    like "Posted 2 days ago" alongside the title/company/location lines.
    Only relative mentions are searched — an absolute date in prose is
    too ambiguous to trust.
    """
    if not text:
        return None
    ref = reference or date.today()
    return _parse_relative(text, ref)
