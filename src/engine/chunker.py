"""Sprint 3, Lesson 1 — split JDs and resume evidence into chunks.

A chunk is the unit of retrieval: one JD requirement (or section) on the
one side, one verified resume bullet (or framework phase) on the other.
Step 4 embeds each chunk and stores it; Lesson 2 searches them.

JD side: reuses the section patterns from
src/engine/jd_skill_extractor.py, with one guard the shared splitter
lacks. The shared splitter treats EVERY line containing a trigger word
as a header — a content line like "dbt and Terraform experience is a
plus." matched the "preferred" pattern and the whole preferred section
vanished from the chunks. So a line only counts as a header here if it
also looks like one: short, and not ending in sentence punctuation.
The pattern ORDER is kept identical to the extractor (required first),
so "Preferred qualifications" still lands in the "required" kind —
the text is preserved, the label is a known quirk, and changing the
shared order is a separate decision.

Resume side: parses the master resume markdown (### job headers plus
"- " bullets) and the company frameworks JSON (one chunk per process
phase). The migration comment claimed the evidence came from
experience[].highlights (11 bullets); the real sources are the
markdown (46 bullets, 5 jobs) and the frameworks file (20 phases).
Client names are an explicit map, not clever parsing — the headers
carry employer, location, role and period in one line.
"""

import logging
import re
from typing import Dict, List

from src.engine.jd_skill_extractor import _SECTION_PATTERNS, skill_ids

logger = logging.getLogger(__name__)

MAX_HEADER_LEN = 80
_SENTENCE_END = (".", "!", "?", ";", ",")

# (substring in the ### header, canonical client name, slug for source_ref)
# Order matters only where substrings could overlap; none do today.
CLIENT_MAP = [
    ("Optum", "Optum", "optum"),
    ("Herc Rentals", "Herc Rentals", "herc"),
    ("Blue Yonder", "Blue Yonder", "blue-yonder"),
    ("Accenture", "Accenture", "accenture"),
    ("Thomson Reuters", "Thomson Reuters", "thomson-reuters"),
]

HEADER_RE = re.compile(r"^###\s+(.+)$")
BULLET_RE = re.compile(r"^-\s+(.+)$")


def _looks_like_header(line: str) -> bool:
    """A header is short and has no sentence-ending punctuation.

    "Preferred qualifications" passes (23 chars, no period).
    "dbt and Terraform experience is a plus." fails (ends with "."),
    so it stays content instead of swallowing the section.
    """
    text = line.strip()
    return bool(text) and len(text) <= MAX_HEADER_LEN and not text.endswith(_SENTENCE_END)


def split_jd_sections(jd_text: str) -> List[Dict]:
    """Split a JD into [{name, text}] by section headers.

    Same patterns and first-match order as jd_skill_extractor's
    _split_sections, plus the header guard. Unmatched text falls into
    "other"; a JD with no headers at all becomes one "other" chunk.
    """
    sections: List[Dict] = []
    current = {"name": "other", "lines": []}
    for line in jd_text.splitlines():
        stripped = line.strip()
        matched = None
        if _looks_like_header(stripped):
            for name, pat in _SECTION_PATTERNS:
                if pat.match(stripped):
                    matched = name
                    break
        if matched:
            if current["lines"]:
                sections.append({"name": current["name"], "text": "\n".join(current["lines"])})
            current = {"name": matched, "lines": []}
        else:
            current["lines"].append(line)
    if current["lines"]:
        sections.append({"name": current["name"], "text": "\n".join(current["lines"])})
    return sections or [{"name": "other", "text": jd_text}]


def chunk_jd(job_id: str, description: str) -> List[Dict]:
    """One chunk per JD section: {job_id, chunk_index, chunk_kind,
    chunk_text, skill_tags}. Empty sections are dropped, never stored."""
    chunks = []
    for index, section in enumerate(split_jd_sections(description or "")):
        text = section["text"].strip()
        if not text:
            continue
        chunks.append(
            {
                "job_id": job_id,
                "chunk_index": index,
                "chunk_kind": section["name"],
                "chunk_text": text,
                "skill_tags": skill_ids(text),
            }
        )
    return chunks


def _parse_job_header(header: str) -> Dict:
    """ "Optum, Florida — Data Engineer, Oct 2025 - Present" ->
    {client, client_slug, role, period}."""
    parts = header.split(" — ", 1)
    head, rest = (parts[0], parts[1]) if len(parts) == 2 else (header, "")
    client, slug = "Unknown", "unknown"
    for needle, name, code in CLIENT_MAP:
        if needle in head:
            client, slug = name, code
            break
    role, period = rest, ""
    if ", " in rest:
        role, period = rest.rsplit(", ", 1)
    return {"client": client, "client_slug": slug, "role": role.strip(), "period": period.strip()}


def parse_resume_markdown(md_text: str) -> List[Dict]:
    """Parse ### job headers + "- " bullets into evidence chunks.

    source_ref is "<slug>:bullet:<n>" with n counting per client,
    exactly the key migration 008 documents. skill_tags come from the
    same deterministic matcher as the JD side.
    """
    chunks: List[Dict] = []
    per_client_count: Dict[str, int] = {}
    current = None
    for line in md_text.splitlines():
        header = HEADER_RE.match(line)
        if header:
            current = _parse_job_header(header.group(1))
            continue
        bullet = BULLET_RE.match(line)
        if bullet and current is not None:
            text = bullet.group(1).strip()
            if not text:
                continue
            slug = current["client_slug"]
            n = per_client_count.get(slug, 0)
            per_client_count[slug] = n + 1
            chunks.append(
                {
                    "source_ref": f"{slug}:bullet:{n}",
                    "source_type": "experience_bullet",
                    "client": current["client"],
                    "role": current["role"],
                    "period": current["period"],
                    "framework_name": None,
                    "chunk_text": text,
                    "skill_tags": skill_ids(text),
                }
            )
    return chunks


def chunk_frameworks(frameworks_data: Dict) -> List[Dict]:
    """One chunk per company process phase.

    source_ref is "framework:<company-slug>:<phase-key>"; the chunk text
    is the phase name plus its daily activity — the citable description
    of what that phase does.
    """
    chunks: List[Dict] = []
    for company_slug, company in (frameworks_data.get("companies") or {}).items():
        phases = company.get("process_framework") or {}
        for phase_key, phase in phases.items():
            name = (phase.get("phase_name") or phase_key).strip()
            activity = (phase.get("daily_activity") or "").strip()
            text = f"{name}: {activity}" if activity else name
            chunks.append(
                {
                    "source_ref": f"framework:{company_slug}:{phase_key}",
                    "source_type": "framework",
                    "client": None,
                    "role": None,
                    "period": None,
                    "framework_name": name,
                    "chunk_text": text,
                    "skill_tags": skill_ids(text),
                }
            )
    return chunks


def chunk_resume_evidence(md_text: str, frameworks_data: Dict) -> List[Dict]:
    """All resume evidence: experience bullets + framework phases."""
    return parse_resume_markdown(md_text) + chunk_frameworks(frameworks_data)
