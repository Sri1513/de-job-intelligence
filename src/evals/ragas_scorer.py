"""RAGAS scoring for the tailoring graph's drafts (Sprint 4, Lesson 3).

Why RAGAS instead of Lesson 2's token check: token overlap cannot
see meaning — a faithful rephrase scores low. RAGAS asks an LLM
judge whether each claim in the draft is supported by the selected
bullets (faithfulness) and whether the draft answers the job
(answer relevancy).

The judge is a seam with two kinds:
- "gemini": the google provider via llm_factory — used in CI,
  where the key is already a secret (GEMINI_API_KEY from env).
  Model from GEMINI_JUDGE_MODEL, default gemini-2.0-flash.
- "ollama": the MSI 7b through Ollama's OpenAI-compatible /v1
  endpoint — used on de-lab. Base URL from OLLAMA_BASE_URL,
  default the Windows Ollama address from the repo config.

RAGAS 0.4's collections metrics accept ONLY RAGAS-native LLMs
built by llm_factory — a LangChain-wrapped LLM is rejected
("Collections metrics only support modern InstructorLLM"), so
there is no LangChain judge anywhere in this module. Embeddings
are RAGAS-native HuggingFaceEmbeddings with the repo's own
all-MiniLM-L6-v2.

Fail-open by design: any judge or embedding failure returns None
scores instead of raising — the runner records "unscored" rather
than failing the whole suite. A missing judge is a data gap, not
a code error.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_BASE_URL = "http://100.68.109.36:11434"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Pinned so RAGAS 0.4.4 imports under the repo's numpy<2 pin:
# datasets 5.x needs pyarrow>=24, which needs numpy>=2. CI and
# de-lab must install "datasets>=4.0.0,<5" and "pyarrow>=15,<19".
RAGAS_PIN = "ragas==0.4.4"
DATASETS_PIN = "datasets>=4.0.0,<5"
PYARROW_PIN = "pyarrow>=15,<19"


def make_judge(kind: str, model: Optional[str] = None):
    """Build the RAGAS-native judge LLM via llm_factory.

    Both judges go through an OpenAI-compatible client — Ollama
    serves /v1 natively, and Gemini offers an OpenAI-compatible
    endpoint too — so there is one code path, not two. (RAGAS's
    own Gemini example uses a LiteLLM client; this avoids that
    extra dependency.) Construction only — no network call here.
    """
    from openai import OpenAI
    from ragas.llms import llm_factory

    if kind == "ollama":
        import instructor

        base_url = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL)
        client = OpenAI(base_url=base_url.rstrip("/") + "/v1", api_key="ollama")
        # Ollama's /v1 does not do function calling, which is
        # instructor's default mode — MD_JSON asks for plain JSON
        # instead. Without this the judge returns no parseable
        # claims and faithfulness scores 0.0 on a verbatim draft.
        return llm_factory(
            model or DEFAULT_OLLAMA_MODEL,
            provider="openai",
            client=client,
            mode=instructor.Mode.MD_JSON,
        )
    if kind == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set (needed for the gemini judge)")
        client = OpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=api_key,
        )
        return llm_factory(
            model or os.environ.get("GEMINI_JUDGE_MODEL", DEFAULT_GEMINI_MODEL),
            provider="openai",
            client=client,
        )
    raise ValueError(f"unknown judge kind: {kind!r} (want 'gemini' or 'ollama')")


def make_embeddings():
    """RAGAS-native MiniLM embeddings (the repo's own model)."""
    from ragas.embeddings import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(model=EMBEDDING_MODEL)


def build_sample(job_question: str, bullets: list[str], draft: str):
    """One RAGAS row: the job is the question, the selected bullets
    are the retrieved contexts, the draft is the response.

    The first context names the candidate. The bullets are sentence
    fragments with an implicit subject ("Built pipelines..."), and
    the draft attaches them to the candidate ("Sri Omkar D built
    pipelines..."). Without the candidate line, a strict NLI judge
    marks every statement unsupported — the name is nowhere in the
    contexts — and faithfulness scores 0.0 on a verbatim draft.
    With it, the same draft scores 1.0. Proven live on de-lab,
    Lesson 3 Step 4.
    """
    from ragas import SingleTurnSample

    return SingleTurnSample(
        user_input=job_question,
        retrieved_contexts=["Candidate: Sri Omkar D.", *bullets],
        response=draft,
    )


def score_sample(sample, judge, embeddings) -> dict[str, Optional[float]]:
    """Score one sample directly with the RAGAS 0.4 collections metrics.

    RAGAS 0.4's collections metrics (Faithfulness, AnswerRelevancy)
    are a new BaseMetric API — they do NOT go through evaluate(),
    which validates against the old Metric base class and rejects
    them ("must be initialised metric objects"). Each metric scores
    the sample directly via .score(). Returns {"faithfulness": x,
    "answer_relevancy": y} with None for any score that could not
    be produced. Never raises.
    """
    from ragas.metrics.collections import AnswerRelevancy, Faithfulness

    if not sample.response or not sample.response.strip():
        return {"faithfulness": None, "answer_relevancy": None}
    try:
        faith = Faithfulness(llm=judge).score(
            user_input=sample.user_input,
            response=sample.response,
            retrieved_contexts=list(sample.retrieved_contexts or []),
        )
        rel = AnswerRelevancy(llm=judge, embeddings=embeddings).score(
            user_input=sample.user_input,
            response=sample.response,
        )
        return {
            "faithfulness": _as_float(_metric_value(faith)),
            "answer_relevancy": _as_float(_metric_value(rel)),
        }
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        # Log the TYPE, not just str(exc): the overnight run's 22
        # failures all printed an empty message, which hid the
        # exception class entirely. exc_info keeps the traceback.
        logger.warning("ragas scoring failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        return {"faithfulness": None, "answer_relevancy": None}


def _metric_value(result: Any) -> Any:
    if isinstance(result, (int, float)):
        return result
    return getattr(result, "value", None)


def score_draft(
    job_question: str,
    bullets: list[str],
    draft: str,
    judge_kind: str = "gemini",
) -> dict[str, Optional[float]]:
    """One-call scorer: build the judge + embeddings, score the draft."""
    try:
        judge = make_judge(judge_kind)
        embeddings = make_embeddings()
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        # Same rule as the scoring path below: log the TYPE, not
        # just str(exc) — an empty message hides the exception class.
        logger.warning("ragas judge setup failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        return {"faithfulness": None, "answer_relevancy": None}
    return score_sample(build_sample(job_question, bullets, draft), judge, embeddings)


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN is not a score
        return None
    return round(number, 3)
