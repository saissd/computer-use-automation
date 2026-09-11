"""The replay result contract.

Four outcomes, not two. The distinction between them is the most important
semantic decision in this system:

  success          the capability did the thing; here are the typed outputs
  business_outcome the app gave a legitimate answer that isn't success
                   ("no such member", "permission denied", "validation failed").
                   The caller needs to know. This is NOT an error.
  needs_human      we stopped on purpose and a person must intervene. A
                   first-class return value, not a side channel — this is what
                   stitches replay to the escalation path.
  failed           something is broken. Stop, and surface enough detail to
                   debug: which step, what was expected, what was observed.

A caller that treats `business_outcome` as an exception will retry forever
against a member that does not exist. A caller that treats `failed` as a
business outcome will silently report garbage to a bank. Hence four.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorClass(StrEnum):
    """Taxonomy of things that go wrong. Drives both handling and reporting."""

    # --- hard failures -----------------------------------------------------
    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_AMBIGUOUS = "element_ambiguous"
    CHECKPOINT_FAILED = "checkpoint_failed"
    STEP_ASSERT_FAILED = "step_assert_failed"
    APP_ERROR = "app_error"
    TIMEOUT = "timeout"
    EXTRACTION_FAILED = "extraction_failed"
    PRECONDITION_FAILED = "precondition_failed"

    # --- policy ------------------------------------------------------------
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUIRED = "approval_required"

    # --- session -----------------------------------------------------------
    SESSION_EXPIRED = "session_expired"
    LEASE_LOST = "lease_lost"

    # --- input -------------------------------------------------------------
    INVALID_INPUT = "invalid_input"

    # --- runtime -----------------------------------------------------------
    INTERNAL = "internal"


RECOVERABLE_CLASSES = frozenset(
    {ErrorClass.TIMEOUT, ErrorClass.SESSION_EXPIRED, ErrorClass.ELEMENT_NOT_FOUND}
)


class ReplayError(BaseModel):
    """A hard failure, shaped for debugging rather than for a stack trace."""

    klass: ErrorClass = Field(alias="class")
    step_id: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: str | None = None
    message: str = ""

    model_config = {"populate_by_name": True}

    def summary(self) -> str:
        where = f" at step {self.step_id!r} ({self.step_intent})" if self.step_id else ""
        detail = ""
        if self.expected or self.observed:
            detail = f" | expected: {self.expected} | observed: {self.observed}"
        return f"[{self.klass}]{where} {self.message}{detail}"


class StepRecord(BaseModel):
    """What happened on one step. The unit of the structured run log."""

    step_id: str
    intent: str
    action: str
    status: Literal["ok", "recovered", "skipped", "failed"]
    started_at: datetime
    duration_ms: int

    locator_tier: int | None = Field(
        None,
        description="0 = primary signal resolved it, 1+ = a fallback was needed. "
        "A rising average across replays is the UI-drift signal.",
    )
    locator_used: str | None = None
    value_redacted: str | None = Field(
        None, description="What was typed, already redacted. Never the raw value."
    )
    extracted: dict[str, Any] | None = None
    recoveries_applied: list[str] = Field(default_factory=list)
    attempts: int = 1
    error: ReplayError | None = None
    screenshot: str | None = None
    note: str | None = None


class Evidence(BaseModel):
    """Pointers to the artifacts of a run, never the sensitive content itself."""

    run_id: str
    directory: str
    log_file: str
    screenshots: list[str] = Field(default_factory=list)
    dom_snapshot: str | None = None
    trace: str | None = None


class ReplayResult(BaseModel):
    """The single value returned to a calling agent."""

    status: Literal["success", "business_outcome", "needs_human", "failed"]

    capability: str = Field(description="capability_id@version that ran")
    run_id: str

    # success
    outputs: dict[str, Any] = Field(default_factory=dict)

    # business_outcome
    outcome: str | None = None
    outcome_detail: str | None = None

    # needs_human
    intervention_id: str | None = None
    reason: str | None = None

    # failed
    error: ReplayError | None = None

    # always
    steps: list[StepRecord] = Field(default_factory=list)
    evidence: Evidence | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """True when the automation behaved correctly.

        Note this includes `business_outcome`: the capability worked, the app
        just had a different answer. Only `failed` and `needs_human` mean the
        automation itself did not complete.
        """
        return self.status in ("success", "business_outcome")

    def summary(self) -> str:
        if self.status == "success":
            return f"SUCCESS {self.capability} -> {self.outputs}"
        if self.status == "business_outcome":
            return f"BUSINESS OUTCOME {self.capability} -> {self.outcome}: {self.outcome_detail}"
        if self.status == "needs_human":
            return f"NEEDS HUMAN {self.capability} -> {self.reason} (intervention {self.intervention_id})"
        return f"FAILED {self.capability} -> {self.error.summary() if self.error else 'unknown'}"

    # -- constructors, so call sites read clearly -------------------------

    @classmethod
    def success(cls, *, capability: str, run_id: str, outputs: dict, **kw) -> "ReplayResult":
        return cls(status="success", capability=capability, run_id=run_id, outputs=outputs, **kw)

    @classmethod
    def business(
        cls, *, capability: str, run_id: str, outcome: str, detail: str | None = None, **kw
    ) -> "ReplayResult":
        return cls(
            status="business_outcome",
            capability=capability,
            run_id=run_id,
            outcome=outcome,
            outcome_detail=detail,
            **kw,
        )

    @classmethod
    def needs_human(
        cls, *, capability: str, run_id: str, intervention_id: str, reason: str, **kw
    ) -> "ReplayResult":
        return cls(
            status="needs_human",
            capability=capability,
            run_id=run_id,
            intervention_id=intervention_id,
            reason=reason,
            **kw,
        )

    @classmethod
    def failure(cls, *, capability: str, run_id: str, error: ReplayError, **kw) -> "ReplayResult":
        return cls(status="failed", capability=capability, run_id=run_id, error=error, **kw)
