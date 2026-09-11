"""The capability artifact schema.

This module is the contract between three parties:

  * the **discovery** run, which accretes a Capability while an LLM drives a UI,
  * the **replay** engine, which executes one deterministically with no LLM,
  * a **human reviewer** or a **calling agent**, who must understand what the
    capability does, what it needs, and what it returns.

Design notes worth defending:

1. Element targeting is a *ranked list of semantic signals*, not a selector
   string. `role + accessible name` is the primary signal because it is the
   same abstraction a screen reader (and therefore a human operator) uses, it
   survives CSS/framework churn, and it exists on desktop surfaces too
   (Windows UIA, macOS AX). CSS is the last resort, never the first.

2. Error handling lives in **data** (`Step.on_error`, `Capability.outcomes`),
   not in engine code. A reviewer can read the JSON and see exactly how the
   capability behaves when the app misbehaves, and it is versioned with the
   artifact rather than buried in a release of the runtime.

3. Nothing here is surface-specific. There is no `Page`, no `selector`, no
   `url` outside of `Target.entry` and the navigate action. That is what lets
   the same schema describe a legacy frameset app and, later, a desktop app.
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# Conditions — the one primitive used for waits, checkpoints, error detectors
# --------------------------------------------------------------------------

ConditionKind = Literal[
    "text_present",
    "text_absent",
    "element_present",
    "element_absent",
    "url_matches",
    "title_matches",
]


class Condition(BaseModel):
    """A predicate evaluated against an Observation.

    One model rather than a discriminated union: conditions are evaluated in a
    single place, they are frequently hand-written by a reviewer, and a flat
    shape keeps the emitted JSON Schema readable. The validator below enforces
    the per-kind required fields that a union would have given us for free.
    """

    kind: ConditionKind
    text: str | None = Field(None, description="Substring for text_present/absent")
    role: str | None = Field(None, description="ARIA role for element_present/absent")
    name: str | None = Field(None, description="Accessible name for element_present/absent")
    pattern: str | None = Field(None, description="Regex for url_matches/title_matches")
    case_sensitive: bool = False

    @model_validator(mode="after")
    def _check_required_fields(self) -> "Condition":
        needs = {
            "text_present": ("text",),
            "text_absent": ("text",),
            "element_present": ("role",),
            "element_absent": ("role",),
            "url_matches": ("pattern",),
            "title_matches": ("pattern",),
        }[self.kind]
        missing = [f for f in needs if getattr(self, f) is None]
        if missing:
            raise ValueError(f"condition kind={self.kind!r} requires {missing}")
        return self

    def describe(self) -> str:
        if self.kind in ("text_present", "text_absent"):
            return f"{self.kind}({self.text!r})"
        if self.kind in ("element_present", "element_absent"):
            return f"{self.kind}(role={self.role!r}, name={self.name!r})"
        return f"{self.kind}({self.pattern!r})"


class WaitSpec(BaseModel):
    """Wait for a *condition*, never a fixed sleep.

    Fixed sleeps are the single most common source of flaky replay: too short
    and it breaks under load, too long and every run pays the cost. A polled
    condition is both faster and more reliable.
    """

    condition: Condition
    timeout_ms: int = 10_000
    poll_ms: int = 200


# --------------------------------------------------------------------------
# Element targeting
# --------------------------------------------------------------------------

LocatorStrategy = Literal[
    "role_name",   # a11y role + accessible name  — portable, preferred
    "label",       # form label association       — portable
    "placeholder", # placeholder text             — web-ish but stable
    "text_exact",  # exact visible text           — links, buttons
    "text_near",   # anchor text + direction      — table-hell survival
    "css",         # raw selector                 — web-only, LAST resort
]


class LocatorSignal(BaseModel):
    """One way of identifying a control. Descriptors hold several, ranked."""

    by: LocatorStrategy
    role: str | None = None
    name: str | None = None
    text: str | None = None
    selector: str | None = None
    anchor: str | None = Field(None, description="Nearby label text for text_near")
    direction: Literal["right", "below", "left", "above"] | None = None
    offset: int = Field(
        1,
        ge=1,
        description="How many cells to move in `direction` from the anchor cell. "
        "offset=1 is the adjacent cell; a grid row often needs 2 or 3.",
    )
    exact: bool = False

    @model_validator(mode="after")
    def _check_required_fields(self) -> "LocatorSignal":
        needs: dict[str, tuple[str, ...]] = {
            "role_name": ("role",),
            "label": ("text",),
            "placeholder": ("text",),
            "text_exact": ("text",),
            "text_near": ("anchor",),
            "css": ("selector",),
        }[self.by]
        missing = [f for f in needs if getattr(self, f) is None]
        if missing:
            raise ValueError(f"locator by={self.by!r} requires {missing}")
        return self

    def describe(self) -> str:
        if self.by == "role_name":
            return f"role={self.role!r} name={self.name!r}"
        if self.by == "text_near":
            return f"near={self.anchor!r} dir={self.direction}"
        if self.by == "css":
            return f"css={self.selector!r}"
        return f"{self.by}={self.text!r}"


class Scope(BaseModel):
    """Where to look. Frame path is what makes legacy framesets tractable."""

    frame_path: list[str] = Field(
        default_factory=list,
        description="Ordered frame names/ids from the top document inward.",
    )
    container_role: str | None = None
    container_name: str | None = None


class Disambiguation(BaseModel):
    """How to pick when a descriptor legitimately matches more than one node.

    If a descriptor matches multiple elements and *no* disambiguation is
    declared, resolution is a hard error. Replay never guesses — guessing is
    how automation silently acts on the wrong row.
    """

    nth: int | None = Field(None, description="0-based index among matches")
    within_row_matching: str | None = Field(
        None, description="Restrict to the table row containing this text"
    )


class CapturedEvidence(BaseModel):
    """What the element actually looked like at record time.

    Not used for matching. It exists so a human reviewing the artifact, or
    debugging a replay failure, can see what the recorder was pointing at.
    """

    role: str | None = None
    name: str | None = None
    tag: str | None = None
    bbox: dict[str, float] | None = None
    frame_url: str | None = None
    recorded_html_snippet: str | None = None


class ElementDescriptor(BaseModel):
    """A ranked, multi-signal description of one control.

    Resolution tries `primary`, then each fallback in order, and records which
    tier succeeded. A rising fallback rate across replays is our UI-drift
    signal — see REPORT.md, Heterogeneity & multi-tenant.
    """

    primary: LocatorSignal
    fallbacks: list[LocatorSignal] = Field(default_factory=list)
    scope: Scope | None = None
    disambiguation: Disambiguation | None = None
    captured: CapturedEvidence | None = None

    def signals(self) -> list[LocatorSignal]:
        return [self.primary, *self.fallbacks]

    def describe(self) -> str:
        return self.primary.describe()


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

ActionType = Literal[
    "navigate",
    "click",
    "type",
    "select",
    "press",
    "extract",
    "wait",
    "assert",
]

RecoveryAction = Literal[
    "dismiss",         # click something (a known interstitial) and continue
    "retry",           # re-run this step
    "wait",            # wait then re-run this step
    "reauth",          # re-establish the session, then re-run this step
    "escalate",        # stop and raise an intervention request
    "return_outcome",  # stop and return a named business outcome (exit 0)
    "fail",            # stop and return a hard failure
]


class Recovery(BaseModel):
    """A declarative handler: when this is observed, do that.

    Ordered. First matching handler wins. Handlers are attached to the step
    where the condition is plausible, which keeps the intent local and
    reviewable rather than scattered through engine code.
    """

    when: Condition
    do: RecoveryAction
    target: ElementDescriptor | None = Field(
        None, description="What to click for do='dismiss'"
    )
    outcome: str | None = Field(
        None, description="Named BusinessOutcome for do='return_outcome'"
    )
    max_attempts: int = 2
    wait_ms: int = 1_000
    note: str | None = None

    @model_validator(mode="after")
    def _check_required_fields(self) -> "Recovery":
        if self.do == "dismiss" and self.target is None:
            raise ValueError("recovery do='dismiss' requires a target to click")
        if self.do == "return_outcome" and not self.outcome:
            raise ValueError("recovery do='return_outcome' requires an outcome name")
        return self


class Step(BaseModel):
    """One ordered action, plus how to verify it and what to do if it misbehaves."""

    id: str
    intent: str = Field(description="Human sentence. This is what makes the artifact reviewable.")
    action: ActionType

    target: ElementDescriptor | None = None
    value: str | None = Field(None, description="Literal, or {{param_name}} template")
    url: str | None = Field(None, description="For action='navigate'; may be templated")
    key: str | None = Field(None, description="For action='press', e.g. 'Enter'")

    extract_as: str | None = Field(None, description="Output name for action='extract'")
    extract_kind: Literal["text", "value", "attribute"] = "text"
    extract_attribute: str | None = None

    pre_wait: WaitSpec | None = None
    post_assert: Condition | None = Field(
        None,
        description="Verify the step actually took effect. Never assume a click worked.",
    )

    on_error: list[Recovery] = Field(default_factory=list)
    timeout_ms: int = 10_000
    retries: int = 1

    @model_validator(mode="after")
    def _check_action_shape(self) -> "Step":
        if self.action in ("click", "type", "select", "extract") and self.target is None:
            raise ValueError(f"step {self.id}: action={self.action!r} requires a target")
        if self.action in ("type", "select") and self.value is None:
            raise ValueError(f"step {self.id}: action={self.action!r} requires a value")
        if self.action == "navigate" and not self.url:
            raise ValueError(f"step {self.id}: action='navigate' requires a url")
        if self.action == "press" and not self.key:
            raise ValueError(f"step {self.id}: action='press' requires a key")
        if self.action == "extract" and not self.extract_as:
            raise ValueError(f"step {self.id}: action='extract' requires extract_as")
        if self.action == "wait" and self.pre_wait is None:
            raise ValueError(f"step {self.id}: action='wait' requires pre_wait")
        if self.action == "assert" and self.post_assert is None:
            raise ValueError(f"step {self.id}: action='assert' requires post_assert")
        return self


# --------------------------------------------------------------------------
# Capability contract
# --------------------------------------------------------------------------

Sensitivity = Literal["public", "internal", "pii", "secret"]
ParamType = Literal["string", "number", "boolean", "money", "date"]


class InputParam(BaseModel):
    """An input the calling agent supplies per invocation.

    `sensitivity` is load-bearing, not decorative: anything above `internal`
    is redacted from artifacts, logs, evidence, and the model's context. See
    cua.policy.redaction.
    """

    name: str
    type: ParamType = "string"
    required: bool = True
    description: str = ""
    pattern: str | None = Field(None, description="Regex the value must match")
    example: str | None = None
    sensitivity: Sensitivity = "internal"


class OutputSpec(BaseModel):
    """A value the capability returns to its caller."""

    name: str
    type: ParamType = "string"
    from_step: str = Field(description="Step id whose extraction produces this")
    required: bool = True
    description: str = ""


class BusinessOutcome(BaseModel):
    """A legitimate non-success answer the caller needs to know about.

    "No such member" is an answer, not a crash. Conflating the two is the
    single most common design mistake in this problem, so outcomes are
    first-class, named, and declared up front on the capability.
    """

    name: str
    description: str
    detect: Condition
    detail_from: ElementDescriptor | None = Field(
        None, description="Optional element whose text becomes the outcome detail"
    )


RiskClass = Literal["read_only", "reversible_write", "irreversible"]


class Risk(BaseModel):
    """How dangerous is invoking this capability?

    `irreversible` is never executed unattended. The policy engine blocks it
    and raises an intervention request unless approval is explicitly supplied
    for that invocation.
    """

    klass: RiskClass = Field("read_only", alias="class")
    requires_approval: bool = False
    rationale: str = ""

    model_config = {"populate_by_name": True}


class Target(BaseModel):
    """What application this runs against."""

    app_id: str = Field(description="Vendor product id, shared across tenants")
    surface: Literal["web", "legacy_web", "desktop"] = "legacy_web"
    entry: str = Field(description="Entry point URL or launch target")
    variant: str | None = Field(
        None, description="Tenant/variant id this artifact was recorded against"
    )


class Provenance(BaseModel):
    """Where this artifact came from. Auditability for a regulated environment."""

    discovered_by: Literal["llm", "human", "hand_authored"] = "llm"
    model: str | None = None
    goal: str | None = None
    run_id: str | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_ref: str | None = None


class Stats(BaseModel):
    """Replay history. Feeds the draft -> approved confidence gate."""

    replays: int = 0
    successes: int = 0
    business_outcomes: int = 0
    failures: int = 0
    last_ok: datetime | None = None
    fallback_resolutions: int = Field(
        0, description="Times a non-primary locator tier was needed — drift signal"
    )

    @property
    def success_rate(self) -> float:
        """Health of the *automation*, not of the data it was pointed at.

        Business outcomes are excluded from the denominator on purpose. A
        capability invoked a hundred times for members who do not exist is
        working perfectly; counting those as misses would make the approval
        gate and the drift report fire on healthy artifacts, which is how a
        health signal gets ignored.
        """
        attempts = self.successes + self.failures
        return self.successes / attempts if attempts else 0.0


class Capability(BaseModel):
    """A reusable, reviewable, parameterized automation an AI agent can invoke."""

    schema_version: str = SCHEMA_VERSION
    id: str = Field(description="Stable slug, e.g. 'lookup_member_balance'")
    name: str = Field(description="Human-readable title")
    version: str = Field("1.0.0", description="Semver. Bump on any step change.")
    status: Literal["draft", "approved", "deprecated"] = "draft"
    description: str = Field(
        description="What this does — the agent-facing tool description."
    )

    target: Target
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    risk: Risk = Field(default_factory=Risk)

    preconditions: list[Condition] = Field(
        default_factory=list, description="Asserted before step 1 runs"
    )
    steps: list[Step]
    checkpoint: Condition = Field(
        description="The success condition. If this fails, the replay failed."
    )
    outcomes: list[BusinessOutcome] = Field(default_factory=list)

    provenance: Provenance = Field(default_factory=Provenance)
    stats: Stats = Field(default_factory=Stats)

    @model_validator(mode="after")
    def _check_referential_integrity(self) -> "Capability":
        step_ids = [s.id for s in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("duplicate step ids")

        for out in self.outputs:
            if out.from_step not in step_ids:
                raise ValueError(f"output {out.name!r} references unknown step {out.from_step!r}")
            step = next(s for s in self.steps if s.id == out.from_step)
            if step.action != "extract":
                raise ValueError(
                    f"output {out.name!r} references step {step.id!r} which is not an extract"
                )
            if step.extract_as != out.name:
                raise ValueError(
                    f"output {out.name!r} does not match step extract_as={step.extract_as!r}"
                )

        outcome_names = {o.name for o in self.outcomes}
        for step in self.steps:
            for rec in step.on_error:
                if rec.do == "return_outcome" and rec.outcome not in outcome_names:
                    raise ValueError(
                        f"step {step.id!r} returns undeclared outcome {rec.outcome!r}"
                    )
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def input_names(self) -> set[str]:
        return {p.name for p in self.inputs}

    def sensitive_input_names(self) -> set[str]:
        return {p.name for p in self.inputs if p.sensitivity in ("pii", "secret")}

    def tool_schema(self) -> dict:
        """Render as a function-calling tool definition for a calling agent.

        This is what makes a saved artifact directly agent-invocable: the
        catalog turns each approved capability into one of these.
        """
        type_map = {
            "string": "string",
            "number": "number",
            "money": "string",
            "date": "string",
            "boolean": "boolean",
        }
        props: dict[str, dict] = {}
        for p in self.inputs:
            spec: dict = {"type": type_map[p.type], "description": p.description}
            if p.pattern:
                spec["pattern"] = p.pattern
            props[p.name] = spec

        returns = ", ".join(f"{o.name} ({o.type})" for o in self.outputs) or "nothing"
        outcomes = ", ".join(o.name for o in self.outcomes)
        desc = self.description
        if outcomes:
            desc += f" May return these business outcomes instead of success: {outcomes}."
        desc += f" Returns: {returns}."

        return {
            "name": self.id,
            "description": desc,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": [p.name for p in self.inputs if p.required],
                "additionalProperties": False,
            },
        }
