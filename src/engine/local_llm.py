"""Local-LLM (qwen2.5:3b) fallback tier — backup-only, queue-only.

This module is the ONLY place the local 3B model is reachable from. It is
deliberately quarantined here: no other code path imports it, it never joins
the generic provider chain (config/llm.yaml), and it only ever runs after
Gemini -> Groq have both failed for a given row (the queue in
src/engine/local_llm_queue.py only processes rows whose ai_status is
PENDING/FAILED because the free chain already failed them).

Scope is exactly three narrow tasks, all validated on 2026-11-05/08:
  1. skill extraction (checklist prompt, closed taxonomy vocabulary),
  2. AI Notes (2-3 sentences, grounded in the JD only),
  3. sponsorship-mention detection (regex pre-pass, model confirmation
     only when the regex finds nothing).

Tailoring, outreach email, market chat, and every other LLM caller are
OUT OF SCOPE — the 3B was validated only on short JSON tasks, never on
long-form prompts.

Accuracy technique (from the Nov 5 test pack + Nov 8 recall-fix findings):
a small model is accurate only when the output space is small (tiny JSON),
the temperature is 0, the vocabulary is closed (it may only pick, never
invent), and mechanical checks surround it. The extraction pattern is:
checklist pass -> deterministic verbatim safety net (word-boundary match,
zero model calls, the recall floor) -> second independent checklist pass,
union of the `true` sets. The 3B is nondeterministic even at temperature 0
(the identical prompt re-run on 2026-11-08 missed a different set of
skills), so no single prompt variant is trusted on its own.

Deliberately ABSENT: any "re-check the rejects" step. Both framings were
proven unsafe on 2026-11-08 — judgment framing ("pass 1 marked these as
NOT required") anchors the model to the rejection (0 of 2 misses
recovered); neutral framing ("were NOT found in pass 1") recovered the
misses once and degenerated into yes-to-everything (invented Python and
PowerShell) on another run. Do not add one.

Transport: stdlib urllib POST to settings.MARKET_CHAT_LLM_URL (the full
Open WebUI route https://llm.sriomkar.com/api/chat/completions). A
browser-like User-Agent is MANDATORY — the endpoint sits behind Cloudflare,
which blocks non-browser signatures (error 1010) before Open WebUI ever
sees the request. Timeout is 150s (cold model load is ~90s). The response
parser accepts the OpenAI shape (choices[0].message.content) first and
Ollama's native shape (message.content) second.

Credentials: the key comes ONLY from settings.MARKET_CHAT_LLM_KEY (the
jobs VM .env). It is never pasted, hardcoded, logged, or stored anywhere
in this module. No new env vars are required.

Step 0c vocabulary-sizing outcome (2026-11-08), refined by the battery:
a single 128-key checklist call FAILS with HTTP 524 (Cloudflare
timeout) on both test JDs, even warm — 128 true/false entries take too
long to generate through the ~100s Cloudflare cap. A 64-key chunk
SUCCEEDS on short JDs (BrickRed 3,473 chars: 109.4s, 64/64 keys, valid
JSON) but 524s on the 7,752-char nSCALE JD even warm, so chunking is
adaptive: 64 keys for JDs up to 4,000 chars, 32 keys above
(_chunk_size_for_jd). Do not raise the chunk size without re-running
the sizing experiment. The 3B also drops ~1 key per 64-key JSON
(AS400 3x, PHI Masking 1x in the battery), so up to 3 missing keys per
chunk are treated as false (logged); more than that still fails.
"""

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from src.core.config import settings
from src.engine.skill_normalizer import (
    _surface_index,
    canonical_name,
    load_taxonomy,
)

logger = logging.getLogger(__name__)

# Browser-like User-Agent is mandatory (see module docstring): Cloudflare
# blocks non-browser signatures with error 1010. Same string class as
# src/dashboard/market_chat.py::_post_json.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

LOCAL_LLM_TIMEOUT_S = 150

# Step 0c outcome (2026-11-08): 128-key single calls 524 through
# Cloudflare on both test JDs, even warm; 64-key chunks succeed on
# short JDs (BrickRed 3,473 chars: 109.4s, 64/64 keys). Two chunks of
# 64 per checklist pass for short JDs — but the battery showed 64-key
# chunks 524 on the 7,752-char nSCALE JD even warm, so long JDs get
# 32-key chunks (4 per pass). The threshold sits between the two
# measured JDs; a 32-key chunk halves the output tokens, which is what
# dominates the call time (input prefill is parallel and fast — the
# warm sponsorship call on the full nSCALE JD took 2.3s).
CHECKLIST_CHUNK_SIZE = 64
CHECKLIST_CHUNK_SIZE_LONG_JD = 32
LONG_JD_CHARS = 4000


def _chunk_size_for_jd(jd_text: str) -> int:
    """Checklist chunk size for this JD: 64 keys for short JDs, 32 for
    long ones (see CHECKLIST_CHUNK_SIZE)."""
    if len(jd_text or "") > LONG_JD_CHARS:
        return CHECKLIST_CHUNK_SIZE_LONG_JD
    return CHECKLIST_CHUNK_SIZE

# Battery finding (2026-11-08, runs 1-2): the 3B drops ~1 key per
# 64-key JSON on first attempt (AS400 dropped 3x, PHI Masking 1x) — a
# long-output reliability issue, not a JD problem; the dropped keys
# were all genuinely false for the JD. A strict "every key present"
# parse therefore fails most jobs on a key that would be false anyway.
# Up to this many missing keys are treated as false (logged); more
# than that is a genuinely broken response and still fails. Extras and
# non-bool values still fail — those are malformed, not omissions.
MAX_MISSING_CHECKLIST_KEYS = 3

# Sponsorship regex (word-boundary: bare "OPT" must not match "option").
# Same alternation as the Nov 5 test pack, with \b guards added and the
# verb/plural forms ("sponsor", "visas") plus EAD / H-1 / permanent
# resident added — "We do not sponsor visas" is the most common phrasing
# and the Nov 5 list missed it entirely.
SPONSORSHIP_PATTERN = re.compile(
    r"\b(?:sponsorship|sponsor|visas|visa|H-1B|H1B|H-1|EAD|"
    r"citizen|authorized to work|work authorization|work authorised|"
    r"employment authorization|green card|permanent resident|OPT|STEM)\b",
    re.IGNORECASE,
)

# Explicit-denial phrases for sponsorship polarity. A matched sentence
# containing one of these means the JD states sponsorship is NOT
# available. Anything else that mentions sponsorship/work authorization
# is reported as "Available" — a known limitation (a bare "must be
# authorized to work" sentence with no denial maps to "Available"; see
# the battery notes in the PR). Bare "do not"/"will not" are deliberately
# NOT here: they would misfire on sentences like "we do not discriminate"
# that happen to mention authorization.
_SPONSORSHIP_DENIAL_PHRASES = (
    "will not provide",
    "will not sponsor",
    "do not sponsor",
    "does not sponsor",
    "not provide",
    "not offer",
    "not sponsor",
    "no sponsorship",
    "cannot sponsor",
    "unable to sponsor",
    "no visa sponsorship",
    "not eligible for sponsorship",
    "sponsorship is not available",
    "sponsorship not available",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_CHECKLIST_PROMPT = """For EACH skill in the list below, decide whether the job description explicitly names it or clearly requires it. Reply with ONLY this JSON, with one true/false entry for EVERY skill in the list, in the same order as the list: {"skills": {"skill1": true, "skill2": false}}. Do not skip any skill. Do not add skills that are not in the list. If unsure, answer false.

Skill list: %s

Job description:
%s"""

_SPONSORSHIP_CONFIRM_PROMPT = """Does this job description mention sponsorship, visas, or work authorization? Reply with ONLY this JSON: {"mentioned": true/false, "quote": "<exact sentence from the description, or null>"}. "mentioned" is true ONLY if the description states sponsorship, visa, work authorization or citizenship requirements. An equal-opportunities / diversity statement does NOT count.

Job description:
%s"""

_AI_NOTES_PROMPT = """Write 2-3 sentences summarizing this job: what the role is, the core skills it requires, and the seniority or experience level. Use only facts stated in the job description; do not invent facts, and do not name skills the description does not mention. Reply with plain text only, no JSON, no markdown.

Job description:
%s"""


class LocalLLMError(RuntimeError):
    """Any failure of the local 3B tier (parse, validation, empty)."""


class LocalLLMUnavailableError(LocalLLMError):
    """Transport-level failure: the tier itself is unreachable.

    Connection errors, HTTP errors (including Cloudflare 1010/524),
    auth failures, timeouts, and empty responses. Callers treat this as
    "the tier is down" (stop the batch, alert once) rather than as a
    per-job data failure.
    """


def load_checklist_vocabulary() -> List[str]:
    """Checklist vocabulary: taxonomy skill names, in taxonomy order."""
    return [skill["name"] for skill in load_taxonomy().get("skills", [])]


# ---------------------------------------------------------------------------
# Transport (single choke point; unit tests stub _chat_complete)
# ---------------------------------------------------------------------------

def _chat_complete(prompt: str, timeout: int = LOCAL_LLM_TIMEOUT_S) -> str:
    """One chat-completions call to the local model. Returns the text."""
    url = settings.MARKET_CHAT_LLM_URL
    key = settings.MARKET_CHAT_LLM_KEY
    model = settings.MARKET_CHAT_LLM_MODEL or "qwen2.5:3b"
    if not url:
        raise LocalLLMUnavailableError(
            "local LLM not configured: MARKET_CHAT_LLM_URL is empty"
        )
    if not key:
        raise LocalLLMUnavailableError(
            "local LLM not configured: MARKET_CHAT_LLM_KEY is empty"
        )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": BROWSER_USER_AGENT,
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - best-effort detail only
            detail = ""
        raise LocalLLMUnavailableError(
            f"local LLM HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - timeouts, DNS, connection
        raise LocalLLMUnavailableError(f"local LLM transport failed: {exc}") from exc
    # OpenAI-compatible shape first, Ollama-native shape second.
    choices = data.get("choices") or []
    if choices:
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
    else:
        text = ((data.get("message") or {}).get("content") or "").strip()
    if not text:
        raise LocalLLMUnavailableError("local LLM returned an empty response")
    return text


def _strip_fences(text: str) -> str:
    """Strip markdown code fences if the model wrapped its JSON in them."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").replace("json\n", "", 1).strip()
    return stripped


def _parse_json_object(text: str) -> Dict[str, Any]:
    """Parse a JSON object, salvaging it from surrounding prose if needed."""
    try:
        parsed = json.loads(_strip_fences(text))
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise LocalLLMError(f"model output is not JSON: {exc}") from exc
        else:
            raise LocalLLMError("model output is not JSON")
    if not isinstance(parsed, dict):
        raise LocalLLMError("model output is not a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# Skill extraction
# ---------------------------------------------------------------------------

def _checklist_chunk(jd_text: str, chunk: List[str]) -> Dict[str, bool]:
    """One checklist call over a chunk of the vocabulary.

    Strict parse: every chunk key must be present (up to
    MAX_MISSING_CHECKLIST_KEYS missing keys are treated as false — the
    3B drops ~1 key per 64-key JSON; see the constant), no extras, all
    values bool. On parse failure, retry once with "Reply with JSON
    only" appended. On transport failure (tier down: 524/timeout/auth),
    retry once with the same prompt — cold-start 524s are the classic
    case (the battery's first call on the 7,752-char nSCALE JD 524'd
    cold); if the retry also fails, the LocalLLMUnavailableError
    propagates so the queue stops the batch instead of marking the row
    FAILED.
    """
    prompt = _CHECKLIST_PROMPT % (json.dumps(chunk), jd_text)
    last_error: Optional[Exception] = None
    for attempt in range(2):
        call_prompt = prompt if attempt == 0 else prompt + "\nReply with JSON only."
        try:
            text = _chat_complete(call_prompt)
        except LocalLLMUnavailableError as exc:
            last_error = exc
            logger.warning(
                "local-llm: checklist chunk attempt %d transport failure: %s",
                attempt + 1, exc,
            )
            continue
        try:
            parsed = _parse_json_object(text)
            skills = parsed.get("skills")
            if not isinstance(skills, dict):
                raise LocalLLMError("checklist response has no 'skills' object")
            missing = [k for k in chunk if k not in skills]
            if missing:
                if len(missing) <= MAX_MISSING_CHECKLIST_KEYS:
                    # The model omitted keys it judges false rather than
                    # writing them out; record them as false and move on.
                    # The second pass + verbatim net cover recall for any
                    # true skill dropped this way.
                    logger.warning(
                        "local-llm: checklist chunk missing %d key(s), "
                        "treating as false: %s",
                        len(missing), missing,
                    )
                    for k in missing:
                        skills[k] = False
                else:
                    raise LocalLLMError(
                        f"checklist missing keys: {missing[:5]}"
                        + ("..." if len(missing) > 5 else "")
                    )
            extra = [k for k in skills if k not in chunk]
            if extra:
                raise LocalLLMError(
                    f"checklist has keys outside the list: {extra[:5]}"
                    + ("..." if len(extra) > 5 else "")
                )
            non_bool = [k for k, v in skills.items() if not isinstance(v, bool)]
            if non_bool:
                raise LocalLLMError(
                    f"checklist values are not true/false: {non_bool[:5]}"
                )
            return {k: bool(skills[k]) for k in chunk}
        except LocalLLMError as exc:
            last_error = exc
            logger.warning("local-llm: checklist chunk attempt %d failed: %s", attempt + 1, exc)
    if isinstance(last_error, LocalLLMUnavailableError):
        raise last_error
    raise LocalLLMError(f"checklist chunk failed after retry: {last_error}")


def checklist_extract(
    jd_text: str,
    vocabulary: List[str],
    chunk_size: Optional[int] = None,
) -> Dict[str, bool]:
    """Checklist extraction over the vocabulary, in chunks.

    Returns {skill_name: true/false} for EVERY vocabulary skill. One
    strict-parsed model call per chunk (plus one retry per failed chunk).
    chunk_size defaults to adaptive: 64 keys for short JDs, 32 for long
    ones (see _chunk_size_for_jd).
    """
    if not jd_text or not jd_text.strip():
        raise LocalLLMError("checklist extraction needs a non-empty job description")
    if not vocabulary:
        raise LocalLLMError("checklist extraction needs a non-empty vocabulary")
    if chunk_size is None:
        chunk_size = _chunk_size_for_jd(jd_text)
    if chunk_size <= 0:
        raise LocalLLMError("checklist chunk_size must be positive")
    merged: Dict[str, bool] = {}
    for start in range(0, len(vocabulary), chunk_size):
        merged.update(_checklist_chunk(jd_text, vocabulary[start:start + chunk_size]))
    return merged


def _name_patterns(vocabulary: List[str]):
    """Word-boundary regexes for the vocabulary names, longest-first.

    Same pattern shape as jd_skill_extractor._compile_patterns:
    (?<![\\w+#])<name>(?![\\w+#]) — so "audit" never matches "auditor".

    Names only, NOT aliases: the checklist and the net operate on the
    same vocabulary (the 128 taxonomy names). Alias matching belongs to
    the normalizer, and alias surface forms are too generic to trust
    verbatim — e.g. the alias "spark" matches "your contributions spark
    real impact" (a verb, not Apache Spark) in the nSCALE JD, and the
    alias "github" matches "GitHub Actions". A names-only net cannot
    make that mistake: "Apache Spark" only matches the full phrase.
    Returns [(pattern, name)].
    """
    forms = sorted(set(vocabulary), key=len, reverse=True)
    return [
        (
            re.compile(r"(?i)(?<![\w+#])" + re.escape(form) + r"(?![\w+#])"),
            form,
        )
        for form in forms
    ]


def verbatim_safety_net(jd_text: str, vocabulary: List[str]) -> List[str]:
    """Deterministic recall floor: every vocabulary skill name that
    appears verbatim (word-boundary match) in the JD, regardless of the
    model.

    Zero model calls. Longest-first with consumed spans, so "PySpark"
    never double-counts as "Spark". Names only (see _name_patterns):
    the model handles alias understanding ("Kafka" -> "Apache Kafka")
    in the checklist passes; the net only catches verbatim name
    mentions the model dropped.
    """
    if not jd_text or not jd_text.strip():
        return []
    consumed = [False] * len(jd_text)
    found: List[str] = []
    for pattern, name in _name_patterns(vocabulary):
        if name in found:
            continue
        for match in pattern.finditer(jd_text):
            start, end = match.span()
            if any(consumed[start:end]):
                continue
            for i in range(start, end):
                consumed[i] = True
            found.append(name)
            break
    return found


def union_checklists(
    pass1: Dict[str, bool], pass2: Dict[str, bool]
) -> List[str]:
    """Union of the `true` sets of two independent checklist passes."""
    return sorted({k for k, v in pass1.items() if v} | {k for k, v in pass2.items() if v})


def normalize_extracted(raw_skills: List[str]) -> List[str]:
    """Post-filter raw skill strings: exact match, then alias map.

    Fuzzy matching is DELIBERATELY not used here. rapidfuzz is declared
    in pyproject.toml but its install on the jobs VM is unverified, and
    the step-0a decision is exact + alias only until that install is
    verified — so this resolves through the surface index directly
    instead of skill_normalizer.normalize_skill (whose fuzzy step engages
    whenever rapidfuzz happens to be importable). Unmatched skills stay
    as free text (their original string), never dropped silently.
    """
    index = _surface_index()
    out: List[str] = []
    for raw in raw_skills:
        key = (raw or "").strip().lower()
        canonical_id = index.get(key) if key else None
        out.append(canonical_name(canonical_id) if canonical_id else raw)
    return out


def extract_skills(
    jd_text: str, vocabulary: Optional[List[str]] = None
) -> List[str]:
    """Full extraction pattern: checklist pass 1 -> verbatim safety net
    -> checklist pass 2, union of the trues, normalized.

    Returns canonical display names (free text for unmatched strings).
    Raises LocalLLMError if either checklist pass fails after retry.
    """
    vocab = vocabulary if vocabulary is not None else load_checklist_vocabulary()
    pass1 = checklist_extract(jd_text, vocab)
    net = verbatim_safety_net(jd_text, vocab)
    pass2 = checklist_extract(jd_text, vocab)
    merged = sorted(set(union_checklists(pass1, pass2)) | set(net))
    return normalize_extracted(merged)


# ---------------------------------------------------------------------------
# Sponsorship detection
# ---------------------------------------------------------------------------

def _matching_sentences(jd_text: str) -> List[str]:
    """Sentences in the JD that mention sponsorship/visa/work authorization."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(jd_text or "") if s.strip()]
    return [s for s in sentences if SPONSORSHIP_PATTERN.search(s)]


def _sponsorship_value_from_sentences(sentences: List[str]) -> str:
    """Polarity from the matched sentences: an explicit denial anywhere
    means "No Sponsorship"; otherwise "Available"."""
    lowered = " ".join(sentences).lower()
    if any(phrase in lowered for phrase in _SPONSORSHIP_DENIAL_PHRASES):
        return "No Sponsorship"
    return "Available"


def sponsorship_check(jd_text: str) -> Dict[str, Any]:
    """Sponsorship-mention detection.

    Regex pre-pass first (zero model risk): a match means mentioned, with
    the matched sentence as the quote — no model call. No match means one
    model confirmation call; an equal-opportunities / diversity statement
    does NOT count.

    Returns {"mentioned": bool, "sponsorship": "Available" |
    "Not Mentioned" | "No Sponsorship", "quote": str | None}.
    """
    if not jd_text or not jd_text.strip():
        raise LocalLLMError("sponsorship check needs a non-empty job description")
    matched = _matching_sentences(jd_text)
    if matched:
        # Quote the denial sentence when one exists, else the first match.
        quote = next(
            (s for s in matched if any(p in s.lower() for p in _SPONSORSHIP_DENIAL_PHRASES)),
            matched[0],
        )
        return {
            "mentioned": True,
            "sponsorship": _sponsorship_value_from_sentences(matched),
            "quote": quote,
        }
    prompt = _SPONSORSHIP_CONFIRM_PROMPT % jd_text
    last_error: Optional[Exception] = None
    for attempt in range(2):
        call_prompt = prompt if attempt == 0 else prompt + "\nReply with JSON only."
        try:
            text = _chat_complete(call_prompt)
        except LocalLLMUnavailableError as exc:
            # Transport failure (cold-start 524 on the long prompt):
            # retry once warm; if it still fails, propagate as tier-down.
            last_error = exc
            logger.warning(
                "local-llm: sponsorship confirmation attempt %d transport failure: %s",
                attempt + 1, exc,
            )
            continue
        try:
            parsed = _parse_json_object(text)
            mentioned = parsed.get("mentioned")
            if not isinstance(mentioned, bool):
                raise LocalLLMError("sponsorship response has no bool 'mentioned'")
            quote = parsed.get("quote")
            if mentioned:
                if not quote or not isinstance(quote, str):
                    raise LocalLLMError("sponsorship response claims a mention with no quote")
                if quote.strip().lower() not in jd_text.lower():
                    raise LocalLLMError("sponsorship quote is not verbatim in the JD")
                return {
                    "mentioned": True,
                    "sponsorship": _sponsorship_value_from_sentences([quote]),
                    "quote": quote.strip(),
                }
            return {"mentioned": False, "sponsorship": "Not Mentioned", "quote": None}
        except LocalLLMError as exc:
            last_error = exc
            logger.warning(
                "local-llm: sponsorship confirmation attempt %d failed: %s", attempt + 1, exc
            )
    if isinstance(last_error, LocalLLMUnavailableError):
        raise last_error
    raise LocalLLMError(f"sponsorship confirmation failed after retry: {last_error}")


# ---------------------------------------------------------------------------
# AI Notes
# ---------------------------------------------------------------------------

def ai_notes(jd_text: str) -> str:
    """2-3 sentence JD summary, grounded in the description only.

    No invented facts, no skills the JD does not mention. One model call;
    an empty reply raises LocalLLMError (the queue marks the job FAILED
    and retries it later). A transport failure (cold-start 524) is
    retried once warm; if it still fails, the LocalLLMUnavailableError
    propagates so the queue stops the batch as tier-down.
    """
    if not jd_text or not jd_text.strip():
        raise LocalLLMError("AI Notes needs a non-empty job description")
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            text = _chat_complete(_AI_NOTES_PROMPT % jd_text)
        except LocalLLMUnavailableError as exc:
            last_error = exc
            logger.warning(
                "local-llm: AI Notes attempt %d transport failure: %s", attempt + 1, exc
            )
            continue
        cleaned = _strip_fences(text).strip()
        if not cleaned:
            raise LocalLLMError("AI Notes call returned empty text")
        return cleaned
    if isinstance(last_error, LocalLLMUnavailableError):
        raise last_error
    raise LocalLLMError("AI Notes failed after retry")
