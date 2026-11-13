# src/protocols/validation.py
"""
Pydantic v2 argument validation for MCP tools (Sprint 2, Batch 1).

Before this module, tools/call passed `arguments` straight to the
dispatcher with no validation at all: the only checks were JSON Schema
dicts in schemas.py (documentation for clients) plus a few manual
`if not job_id: raise ValueError` guards inside handlers. A mistyped
field, a missing required argument, or an out-of-range limit therefore
reached business logic — or crashed it with an AttributeError that the
-32603 handler then leaked back to the client.

Each tool gets one Pydantic model. The models mirror the advertised
schemas in schemas.py *and* the fields handlers actually read, because
the two disagree in places:

- `export_tailored_resume` handlers also read `tailored_content`,
  `company_name`/`company`, `job_title`/`title`, and `slot_headers`
  (none advertised). They are optional model fields now, so existing
  callers keep working.
- `run_application_worker` also reads `max_jobs` (alias for `limit`).
- `retry_job_evaluations` also reads `job_ids` and `category`.
- `run_batch_ingestion` schema defaults ("Data Engineer Spark" /
  "Remote") are NOT what runs: the handler defaults to "Data Engineer" /
  "United States". Model defaults follow the handler (behavior-preserving);
  schemas.py was aligned to match.
- `stage_agent_jobs` and `export_tailored_resume` historically accepted
  a JSON *string* for `jobs` / `llm_payload` (handlers json.loads it).
  A BeforeValidator preserves that legacy form while still validating
  the parsed contents.

`extra="forbid"` is deliberate: an unknown field is now rejected instead
of silently dropped, so a typo'd argument name fails loudly at the door
instead of quietly doing nothing.

Handlers are untouched: `validate_tool_arguments` returns a plain dict
(`model_dump()`, defaults applied), so the dispatch layer and every
handler keep receiving exactly the shape they already handle.
"""
import json
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator


class ToolArgumentError(Exception):
    """Raised when a tools/call argument set fails Pydantic validation.

    Carries the tool name and a JSON-safe list of field errors
    (``[{"field": ..., "message": ...}]``) so the transport layer can
    answer JSON-RPC -32602 without leaking internals.
    """

    def __init__(self, tool_name: str, errors: list[dict[str, str]]):
        super().__init__(f"Invalid arguments for tool '{tool_name}'")
        self.tool_name = tool_name
        self.errors = errors


def _coerce_json_string(value: Any) -> Any:
    """BeforeValidator: parse a JSON string into its value (legacy compat).

    Handlers historically accepted a JSON string for `jobs` and
    `llm_payload`. If the string is not valid JSON, return it unchanged
    so Pydantic reports a clean type error for the field.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


class _StrictModel(BaseModel):
    """Base for all tool argument models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class PrepareJobTailoringPromptArgs(_StrictModel):
    job_id: str = Field(..., min_length=1)


class ExportTailoredResumeArgs(_StrictModel):
    llm_payload: Annotated[dict[str, Any] | None, BeforeValidator(_coerce_json_string)] = None
    # Legacy alias the handler falls back to when llm_payload is absent.
    tailored_content: Annotated[dict[str, Any] | None, BeforeValidator(_coerce_json_string)] = None
    document_title: str | None = None
    # Optional fields the handler reads but the schema never advertised.
    company_name: str | None = None
    company: str | None = None
    job_title: str | None = None
    title: str | None = None
    slot_headers: Any | None = None

    @model_validator(mode="after")
    def _require_a_payload(self) -> "ExportTailoredResumeArgs":
        if self.llm_payload is None and self.tailored_content is None:
            raise ValueError("llm_payload or tailored_content is required")
        return self


class GetJobDetailsArgs(_StrictModel):
    job_id: str = Field(..., min_length=1)


class SearchSavedJobsArgs(_StrictModel):
    category: str = "data_engineering"
    min_score: int = Field(0, ge=0, le=100)
    status: str = "ALL"
    limit: int = Field(10, ge=1, le=100)


class QueueJobForApplicationArgs(_StrictModel):
    job_id: str = Field(..., min_length=1)


class RunApplicationWorkerArgs(_StrictModel):
    limit: int = Field(3, ge=1, le=20)
    # Legacy alias the handler reads as a fallback for limit.
    max_jobs: int | None = Field(None, ge=1, le=20)
    headless: bool = True


class RetryJobEvaluationsArgs(_StrictModel):
    limit: int = Field(5, ge=1, le=100)
    job_ids: list[str] | None = None
    category: str = "data_engineering"


class RunBatchIngestionArgs(_StrictModel):
    # Defaults follow the handler (what actually runs), not the old
    # schema text (which advertised "Data Engineer Spark" / "Remote").
    search_term: str = "Data Engineer"
    location: str = "United States"
    results_wanted: int = Field(15, ge=1, le=100)
    hours_old: int = Field(48, ge=1, le=720)
    job_category: str = "data_engineering"
    c2c_only: bool = False


class StageAgentJobItem(_StrictModel):
    """One staged posting. Title/company/job_url are required; the rest
    is typed where known. Extra fields are ALLOWED (not forbidden):
    the tool's own contract preserves arbitrary detail fields
    (workplace, duration, rate text, poster/recruiter, apply method)
    into the row's metadata, so a strict item model would break real
    callers. model_dump() carries the extras through untouched."""

    model_config = ConfigDict(extra="allow")

    title: str = Field(..., min_length=1)
    company: str = Field(..., min_length=1)
    job_url: str = Field(..., min_length=1)
    location: str | None = None
    source: str | None = None
    description: str | None = None
    employment_type: str | None = None
    sponsorship: str | None = None
    date_posted: str | None = None


class StageAgentJobsArgs(_StrictModel):
    # The schema docstring says "at most 100 per call"; enforce it.
    jobs: Annotated[list[StageAgentJobItem], BeforeValidator(_coerce_json_string)] = Field(
        ..., min_length=1, max_length=100
    )
    job_category: str = "data_engineering"
    run_evaluation: bool = True
    default_source: str = "Agent"


class ProcessJobAlertDraftArgs(_StrictModel):
    whatsapp_text: str = Field(..., min_length=1)
    helper_name: str = Field(..., min_length=1)
    helper_email: str | None = None
    helper_company: str | None = None
    generate_assets: bool = False


class RunEmailPipelineArgs(_StrictModel):
    limit: int = Field(5, ge=1, le=100)
    job_category: str = "data_engineering"


TOOL_ARGUMENT_MODELS: dict[str, type[_StrictModel]] = {
    "prepare_job_tailoring_prompt": PrepareJobTailoringPromptArgs,
    "fetch_job_for_tailoring": PrepareJobTailoringPromptArgs,
    "export_tailored_resume": ExportTailoredResumeArgs,
    "generate_tailored_resume": ExportTailoredResumeArgs,
    "get_job_details": GetJobDetailsArgs,
    "search_saved_jobs": SearchSavedJobsArgs,
    "queue_job_for_application": QueueJobForApplicationArgs,
    "run_application_worker": RunApplicationWorkerArgs,
    "retry_job_evaluations": RetryJobEvaluationsArgs,
    "run_batch_ingestion": RunBatchIngestionArgs,
    "stage_agent_jobs": StageAgentJobsArgs,
    "process_job_alert_draft": ProcessJobAlertDraftArgs,
    "run_email_pipeline": RunEmailPipelineArgs,
}


def validate_tool_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
    """Validate a tools/call argument set; return the validated dict.

    Unknown tools pass through untouched (dispatch reports them as an
    error result, unchanged). A non-dict argument set or any Pydantic
    failure raises ToolArgumentError with field-level errors.
    """
    model = TOOL_ARGUMENT_MODELS.get(tool_name)
    if model is None:
        return arguments if isinstance(arguments, dict) else {}
    if not isinstance(arguments, dict):
        raise ToolArgumentError(
            tool_name,
            [{"field": "arguments", "message": "arguments must be an object"}],
        )
    try:
        validated = model.model_validate(arguments)
    except ValidationError as exc:
        errors = [
            {
                "field": ".".join(str(part) for part in err["loc"]) or "arguments",
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        raise ToolArgumentError(tool_name, errors) from exc
    return validated.model_dump()
