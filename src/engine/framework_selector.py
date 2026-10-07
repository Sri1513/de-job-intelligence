"""Framework selection: one shared LLM-judged selector used by both the
dashboard tailoring path and the WhatsApp alert path.

Decision algorithm:
  1. Thin check (no LLM spent): input too short / title-only -> default framework.
  2. LLM judgment (one call): the model picks from the registry menu and
     returns {"framework": "<id>", "rationale": "<one line>"}.
  3. Validation: unknown id or LLM failure -> default framework.

The default framework is `herc_rentals` (modern-stack story fits the widest
set of generic DE postings); Optum is the specialist pick the model chooses
for healthcare / legacy-migration JDs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

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


def _build_menu(registry: dict[str, Any]) -> str:
    lines = []
    for fw_id, entry in registry.items():
        if fw_id == "default_framework" or not isinstance(entry, dict):
            continue
        lines.append(
            f"- {fw_id}: {entry.get('description', '')} "
            f"(best for: {', '.join(entry.get('primary_for', []))})"
        )
    return "\n".join(lines)


SELECTION_PROMPT = """You are choosing which resume framework best fits a job posting.
Pick exactly ONE framework from the menu below — the one whose story and tech stack
most closely match what the job asks for.

FRAMEWORK MENU:
{menu}

DEFAULT FRAMEWORK: {default_id}
If no framework fits clearly, choose the default.

JOB TITLE: {title}

JOB TEXT:
{jd_text}

Selection guidance:
- Healthcare, HIPAA, clinical, payer/provider, or legacy ETL migration (DataStage, SSIS, modernization) -> optum
- Telematics, IoT, or modern stack (PySpark, Airflow, Kinesis, Databricks streaming) -> herc_rentals
- Match on the closest story, not keyword counting. One framework only.

Return a STRICT JSON object (no markdown, no backticks):
{{"framework": "<framework_id>", "rationale": "<one short sentence>"}}
"""


def select_framework(jd_text: str, title: str = "") -> dict[str, Any]:
    """Selects the framework for a JD (or WhatsApp fragment).

    Returns {"framework_id": str, "rationale": str, "thin": bool}.
    Never raises: any failure falls back to the default framework.
    """
    registry = load_registry()
    default_id = default_framework_id()

    text = (jd_text or "").strip()
    if len(text) < THIN_TEXT_CHARS:
        return {
            "framework_id": default_id,
            "rationale": "Input too thin to judge; using default framework.",
            "thin": True,
        }

    prompt = SELECTION_PROMPT.format(
        menu=_build_menu(registry),
        default_id=default_id,
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

    if chosen not in registry or chosen == "default_framework":
        logger.warning(f"Framework selection returned unknown id '{chosen}'; using default.")
        return {
            "framework_id": default_id,
            "rationale": f"Model returned unknown framework '{chosen}'; using default.",
            "thin": False,
        }

    return {"framework_id": chosen, "rationale": rationale, "thin": False}
