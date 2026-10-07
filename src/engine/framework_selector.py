"""Framework selection: one shared LLM-judged selector used by both the
dashboard tailoring path and the WhatsApp alert path.

Routing model (per-slot):
  - Slot 1 (current role) is decided per JD: healthcare JD -> optum,
    everything else -> herc_rentals. The LLM makes this binary call.
  - Slots 2-4 are FIXED: blue_yonder, accenture, thomson_reuters.

Decision algorithm for slot 1:
  1. Thin check (no LLM spent): input too short / title-only -> herc_rentals.
  2. LLM judgment (one call): healthcare or not -> optum | herc_rentals.
  3. Validation: unexpected answer or LLM failure -> herc_rentals.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.core.config import settings
from src.engine.llm_text import generate_text

logger = logging.getLogger(__name__)

CONFIG_DIR = getattr(settings, "CONFIG_DIR", Path(__file__).resolve().parents[2] / "config")
FRAMEWORKS_DIR = CONFIG_DIR / "frameworks"

# Below this length (after stripping), the input is treated as title-only:
# not worth an LLM call, go straight to the default framework.
THIN_TEXT_CHARS = 120

_REGISTRY_CACHE: dict[str, Any] | None = None
_FRAMEWORK_CACHE: dict[str, dict] = {}


def load_registry() -> dict[str, Any]:
    """Loads the framework registry (id -> {file, kind, description, ...})."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        registry_file = FRAMEWORKS_DIR / "registry.json"
        try:
            _REGISTRY_CACHE = json.loads(registry_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error loading framework registry at {registry_file}: {e}")
            _REGISTRY_CACHE = {}
    return _REGISTRY_CACHE


def default_framework_id() -> str:
    return str(load_registry().get("default_framework", "herc_rentals"))


def load_framework(framework_id: str) -> dict[str, Any]:
    """Loads a single framework document by id (cached)."""
    if framework_id not in _FRAMEWORK_CACHE:
        registry = load_registry()
        entry = registry.get(framework_id, {})
        fw_file = FRAMEWORKS_DIR / entry.get("file", f"{framework_id}.json")
        try:
            _FRAMEWORK_CACHE[framework_id] = json.loads(fw_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error loading framework {framework_id} at {fw_file}: {e}")
            _FRAMEWORK_CACHE[framework_id] = {}
    return _FRAMEWORK_CACHE[framework_id]


SELECTION_PROMPT = """You are classifying a job posting for resume tailoring.
Answer ONE question: is this a HEALTHCARE data role?

Healthcare signals include: healthcare, HIPAA, HITRUST, PHI, clinical,
payer, provider, claims, pharmacy, hospital, patient data.

- If YES -> return {{"framework": "optum", "rationale": "<one short sentence>"}}
- If NO -> return {{"framework": "herc_rentals", "rationale": "<one short sentence>"}}

Nothing else is a valid answer. When in doubt, answer NO.

JOB TITLE: {title}

JOB TEXT:
{jd_text}

Return a STRICT JSON object (no markdown, no backticks):
{{"framework": "optum", "rationale": "<one short sentence>"}}
"""


def select_framework(jd_text: str, title: str = "") -> dict[str, Any]:
    """Decides slot 1's framework: optum for healthcare JDs, herc_rentals
    for everything else.

    Returns {"framework_id": str, "rationale": str, "thin": bool}.
    Never raises: any failure falls back to herc_rentals.
    """
    default_id = default_framework_id()

    text = (jd_text or "").strip()
    if len(text) < THIN_TEXT_CHARS:
        return {
            "framework_id": default_id,
            "rationale": "Input too thin to judge; using default framework.",
            "thin": True,
        }

    prompt = SELECTION_PROMPT.format(
        title=title or "Unknown",
        jd_text=text[:4000],
    )

    try:
        raw = generate_text(
            prompt, json_mode=True, temperature=0.1, task="framework-select"
        )
        data = json.loads(raw)
        chosen = str(data.get("framework", "")).strip()
        rationale = str(data.get("rationale", "")).strip()
    except Exception as e:
        logger.warning(f"Framework selection LLM call failed ({e}); using default.")
        return {
            "framework_id": default_id,
            "rationale": "LLM selection unavailable; using default framework.",
            "thin": False,
        }

    if chosen not in ("optum", "herc_rentals"):
        logger.warning(f"Framework selection returned unexpected id '{chosen}'; using default.")
        return {
            "framework_id": default_id,
            "rationale": f"Model returned unexpected framework '{chosen}'; using default.",
            "thin": False,
        }

    return {"framework_id": chosen, "rationale": rationale, "thin": False}


def select_slot_frameworks(jd_text: str, title: str = "") -> dict[str, Any]:
    """Returns the framework id for each resume slot.

    Slot 1 is decided per JD (healthcare -> optum, else herc_rentals);
    slots 2-4 are fixed per the registry's slots config.
    """
    registry = load_registry()
    slots_cfg = registry.get("slots", {})
    slot1 = select_framework(jd_text, title)

    result: dict[str, Any] = {
        "job1": slot1["framework_id"],
        "job1_rationale": slot1["rationale"],
    }
    for slot in ("job2", "job3", "job4"):
        result[slot] = slots_cfg.get(slot, {}).get("fixed", "")
    return result
