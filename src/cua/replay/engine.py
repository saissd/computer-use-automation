"""Deterministic replay — the path an AI agent triggers in production.

No model is consulted. The capability is interpreted exactly as recorded, and
every decision the engine makes is either declared in the artifact or is a
refusal to proceed.

The interesting design is the order in which a step's aftermath is judged,
because it is what keeps the three outcome classes from collapsing into each
other:

    1. step.on_error handlers   most specific — declared on this step
    2. capability.outcomes      global business detectors
    3. step.post_assert         did the step actually do what it claimed
    4. capability.checkpoint    did the whole flow reach the intended state

A "no such member" banner is caught at (1) or (2) and returned as a business
outcome with exit status success. The same page, if nothing declared it, would
fail at (3) as a hard failure with the expected/observed pair attached. That
difference is the entire point: the caller must be able to tell "the member
does not exist" from "our automation is broken", and the artifact author is
the one who decides which is which.

Determinism comes from four rules:
    * no model in the loop, fixed step order;
    * waits are on conditions, never fixed sleeps;
    * locator resolution is unique-or-error — a descriptor matching two
      elements is a defect, not a coin flip;
    * every step verifies its own post-state before the next one runs.
"""

import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from cua.core.models import Capability, Condition, InputParam, Recovery, Step
from cua.core.results import (
    ErrorClass,
    Evidence,
    ReplayError,
    ReplayResult,
    StepRecord,
)
from cua.evidence.writer import EvidenceWriter
from cua.policy.engine import PolicyEngine
from cua.policy.redaction import Redactor
from cua.session.intervention import QUEUE, InterventionQueue, diff_observations
from cua.session.lease import LeaseLost, SessionLease
from cua.surface.base import Ambiguous, NotFound, Resolved
from cua.surface.web import WebSurface

TEMPLATE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

EscalationMode = Literal["return", "wait"]


class InputValidationError(ValueError):
    pass


@dataclass
class _StepOutcome:
    kind: Literal["ok", "business", "escalate", "fail"]
    record: StepRecord
    outcome: str | None = None
    detail: str | None = None
    error: ReplayError | None = None
    reason: str | None = None
    escalation_kind: str = "unrecoverable_error"


def validate_inputs(capability: Capability, inputs: dict[str, Any]) -> dict[str, str]:
    """Check the caller's arguments against the declared contract.

    Done before the browser is touched. An invalid member id should cost
    nothing and fail with a clear message, not halfway through a flow.
    """
    clean: dict[str, str] = {}
    declared = {p.name: p for p in capability.inputs}

    unknown = set(inputs) - set(declared)
    if unknown:
        raise InputValidationError(f"unknown input(s): {sorted(unknown)}")

    for name, spec in declared.items():
        if name not in inputs or inputs[name] is None or inputs[name] == "":
            if spec.required:
                raise InputValidationError(f"missing required input {name!r}")
            continue
        value = str(inputs[name])
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            raise InputValidationError(
                f"input {name!r} does not match required pattern {spec.pattern!r}"
            )
        if spec.type in ("number", "money"):
            try:
                float(value.replace(",", "").replace("$", ""))
            except ValueError:
                raise InputValidationError(
                    f"input {name!r} must be numeric, got {value!r}"
                ) from None
        clean[name] = value
    return clean


def substitute(template: str | None, inputs: dict[str, str]) -> str | None:
    """Fill {{param}} placeholders. Missing parameters are a hard error."""
    if template is None:
        return None

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in inputs:
            raise InputValidationError(f"step references undeclared input {key!r}")
        return inputs[key]

    return TEMPLATE.sub(repl, template)


class ReplayEngine:
    def __init__(
        self,
        surface: WebSurface,
        policy: PolicyEngine,
        lease: SessionLease,
        evidence: EvidenceWriter,
        *,
        queue: InterventionQueue | None = None,
        escalation_mode: EscalationMode = "return",
        escalation_timeout_s: float = 600.0,
        reauth: Callable[[], Awaitable[bool]] | None = None,
    ):
        self.surface = surface
        self.policy = policy
        self.lease = lease
        self.evidence = evidence
        self.queue = queue or QUEUE
        self.escalation_mode = escalation_mode
        self.escalation_timeout_s = escalation_timeout_s
        self.reauth = reauth

    # -- main entry point --------------------------------------------------

    async def run(
        self,
        capability: Capability,
        inputs: dict[str, Any],
        *,
        approved: bool = False,
    ) -> ReplayResult:
        run_id = self.evidence.run_id
        started = time.monotonic()
        records: list[StepRecord] = []
        outputs: dict[str, Any] = {}

        # 1. Validate the contract before touching anything.
        try:
            clean = validate_inputs(capability, inputs)
        except InputValidationError as exc:
            err = ReplayError(
                **{"class": ErrorClass.INVALID_INPUT},
                message=str(exc),
            )
            self.evidence.log("input_invalid", error=str(exc))
            return self._finish(
                ReplayResult.failure(capability=capability.ref, run_id=run_id, error=err),
                records,
                started,
            )

        # Sensitive values go into the redactor before any logging happens.
        for param in capability.inputs:
            if param.sensitivity in ("pii", "secret") and param.name in clean:
                self.evidence.redactor.add_value(clean[param.name])

        self.evidence.log(
            "replay_start",
            capability=capability.ref,
            status=capability.status,
            inputs={
                p.name: Redactor.for_logging(p.name, clean.get(p.name, ""), p.sensitivity)
                for p in capability.inputs
                if p.name in clean
            },
            escalation_mode=self.escalation_mode,
        )

        # 2. Gate the whole capability on its declared risk.
        if capability.risk.klass == "irreversible" and not approved:
            if not self.policy.config.allow_irreversible:
                return await self._escalate(
                    capability,
                    None,
                    kind="approval_required",
                    reason=(
                        f"capability {capability.ref} is classified irreversible "
                        "and was invoked without approval"
                    ),
                    records=records,
                    started=started,
                    inputs=clean,
                )

        # 3. Preconditions.
        for cond in capability.preconditions:
            if not await self.surface.check(cond):
                err = ReplayError(
                    **{"class": ErrorClass.PRECONDITION_FAILED},
                    expected=cond.describe(),
                    observed="not satisfied",
                    message="precondition not met at start of replay",
                )
                await self._capture_failure_evidence("precondition")
                self.evidence.log("precondition_failed", condition=cond.describe())
                return self._finish(
                    ReplayResult.failure(
                        capability=capability.ref, run_id=run_id, error=err
                    ),
                    records,
                    started,
                )

        # 4. Steps.
        for step in capability.steps:
            outcome = await self._run_step(capability, step, clean, outputs, approved)
            records.append(outcome.record)

            if outcome.kind == "ok":
                continue

            if outcome.kind == "business":
                self.evidence.log(
                    "business_outcome", outcome=outcome.outcome, step=step.id
                )
                return self._finish(
                    ReplayResult.business(
                        capability=capability.ref,
                        run_id=run_id,
                        outcome=outcome.outcome or "unknown",
                        detail=outcome.detail,
                        outputs=outputs,
                    ),
                    records,
                    started,
                )

            if outcome.kind == "escalate":
                return await self._escalate(
                    capability,
                    step,
                    kind=outcome.escalation_kind,
                    reason=outcome.reason or "replay could not proceed",
                    records=records,
                    started=started,
                    inputs=clean,
                    resume_from=step,
                    remaining=capability.steps[capability.steps.index(step) :],
                    outputs=outputs,
                )

            # hard failure
            await self._capture_failure_evidence(f"step_{step.id}")
            self.evidence.log("step_failed", step=step.id, error=outcome.error.summary() if outcome.error else "")
            return self._finish(
                ReplayResult.failure(
                    capability=capability.ref,
                    run_id=run_id,
                    error=outcome.error
                    or ReplayError(**{"class": ErrorClass.INTERNAL}, step_id=step.id),
                ),
                records,
                started,
            )

        # 5. Checkpoint — did we actually arrive where the capability claims?
        if not await self.surface.wait_for(capability.checkpoint, 5_000):
            observed = await self._describe_current_state()
            err = ReplayError(
                **{"class": ErrorClass.CHECKPOINT_FAILED},
                expected=capability.checkpoint.describe(),
                observed=observed,
                message="all steps ran but the success condition was not met",
            )
            await self._capture_failure_evidence("checkpoint")
            self.evidence.log("checkpoint_failed", expected=capability.checkpoint.describe())
            return self._finish(
                ReplayResult.failure(capability=capability.ref, run_id=run_id, error=err),
                records,
                started,
            )

        # 6. Declared outputs must all be present.
        missing = [o.name for o in capability.outputs if o.required and o.name not in outputs]
        if missing:
            err = ReplayError(
                **{"class": ErrorClass.EXTRACTION_FAILED},
                expected=f"outputs {missing}",
                observed=f"got {sorted(outputs)}",
                message="checkpoint passed but a declared output was not extracted",
            )
            await self._capture_failure_evidence("outputs")
            return self._finish(
                ReplayResult.failure(capability=capability.ref, run_id=run_id, error=err),
                records,
                started,
            )

        self.evidence.log("replay_success", outputs=outputs)
        return self._finish(
            ReplayResult.success(capability=capability.ref, run_id=run_id, outputs=outputs),
            records,
            started,
        )

    # -- one step ----------------------------------------------------------

    async def _run_step(
        self,
        capability: Capability,
        step: Step,
        inputs: dict[str, str],
        outputs: dict[str, Any],
        approved: bool,
    ) -> _StepOutcome:
        started_at = datetime.now(timezone.utc)
        t0 = time.monotonic()
        record = StepRecord(
            step_id=step.id,
            intent=step.intent,
            action=step.action,
            status="ok",
            started_at=started_at,
            duration_ms=0,
        )
        attempts = 0
        recoveries_used: dict[str, int] = {}

        while True:
            attempts += 1
            record.attempts = attempts

            # The lease check that makes "cede control" real.
            try:
                self.lease.assert_automation()
            except LeaseLost as exc:
                record.status = "failed"
                record.duration_ms = int((time.monotonic() - t0) * 1000)
                return _StepOutcome(
                    "escalate",
                    record,
                    reason=str(exc),
                    escalation_kind="unrecoverable_error",
                )

            result = await self._attempt_step(capability, step, inputs, outputs, approved, record)

            # 1. Step-local handlers win: most specific, declared here.
            handled = await self._match_recovery(step, result)
            if handled is not None:
                rec, cond_desc = handled
                used = recoveries_used.get(cond_desc, 0)
                if used >= rec.max_attempts:
                    record.status = "failed"
                    record.duration_ms = int((time.monotonic() - t0) * 1000)
                    return _StepOutcome(
                        "fail",
                        record,
                        error=ReplayError(
                            **{"class": ErrorClass.INTERNAL},
                            step_id=step.id,
                            step_intent=step.intent,
                            expected=f"recovery {rec.do!r} to clear {cond_desc}",
                            observed=f"condition still present after {used} attempts",
                            message="recovery handler exhausted",
                        ),
                    )
                recoveries_used[cond_desc] = used + 1
                record.recoveries_applied.append(f"{cond_desc}->{rec.do}")

                action = await self._apply_recovery(rec, step)
                if action == "continue":
                    record.status = "recovered"
                    continue
                if action == "business":
                    record.status = "recovered"
                    record.duration_ms = int((time.monotonic() - t0) * 1000)
                    detail = await self._outcome_detail(capability, rec.outcome)
                    return _StepOutcome(
                        "business", record, outcome=rec.outcome, detail=detail
                    )
                if action == "escalate":
                    record.status = "failed"
                    record.duration_ms = int((time.monotonic() - t0) * 1000)
                    return _StepOutcome(
                        "escalate",
                        record,
                        reason=rec.note or f"declared escalation on {cond_desc}",
                        escalation_kind="unrecoverable_error",
                    )
                if action == "fail":
                    record.status = "failed"
                    record.duration_ms = int((time.monotonic() - t0) * 1000)
                    return _StepOutcome(
                        "fail",
                        record,
                        error=ReplayError(
                            **{"class": ErrorClass.APP_ERROR},
                            step_id=step.id,
                            step_intent=step.intent,
                            expected="no error condition",
                            observed=cond_desc,
                            message=rec.note or "declared hard failure",
                        ),
                    )

            # 2. Global business detectors.
            hit = await self._match_outcome(capability)
            if hit is not None:
                name, detail = hit
                record.status = "ok"
                record.duration_ms = int((time.monotonic() - t0) * 1000)
                return _StepOutcome("business", record, outcome=name, detail=detail)

            # 3. Did the attempt itself fail?
            if result is not None:
                if step.retries and attempts <= step.retries and result.klass in (
                    ErrorClass.ELEMENT_NOT_FOUND,
                    ErrorClass.TIMEOUT,
                ):
                    record.status = "recovered"
                    record.note = f"retry {attempts} after {result.klass}"
                    continue
                record.status = "failed"
                record.error = result
                record.duration_ms = int((time.monotonic() - t0) * 1000)
                kind = "escalate" if result.klass in (
                    ErrorClass.POLICY_DENIED,
                    ErrorClass.APPROVAL_REQUIRED,
                    ErrorClass.SESSION_EXPIRED,
                ) else "fail"
                return _StepOutcome(
                    kind,
                    record,
                    error=result,
                    reason=result.message,
                    escalation_kind=(
                        "approval_required"
                        if result.klass == ErrorClass.APPROVAL_REQUIRED
                        else "policy_block"
                        if result.klass == ErrorClass.POLICY_DENIED
                        else "session_expired"
                    ),
                )

            # 4. Post-assertion: never assume the click worked.
            if step.post_assert is not None:
                ok = await self.surface.wait_for(step.post_assert, min(step.timeout_ms, 8_000))
                if not ok:
                    if attempts <= step.retries:
                        record.status = "recovered"
                        record.note = "post-assert failed, retrying"
                        continue
                    observed = await self._describe_current_state()
                    record.status = "failed"
                    record.duration_ms = int((time.monotonic() - t0) * 1000)
                    return _StepOutcome(
                        "fail",
                        record,
                        error=ReplayError(
                            **{"class": ErrorClass.STEP_ASSERT_FAILED},
                            step_id=step.id,
                            step_intent=step.intent,
                            expected=step.post_assert.describe(),
                            observed=observed,
                            message="step ran but did not produce the expected state",
                        ),
                    )

            record.duration_ms = int((time.monotonic() - t0) * 1000)
            self.evidence.log(
                "step_ok",
                step=step.id,
                intent=step.intent,
                action=step.action,
                tier=record.locator_tier,
                locator=record.locator_used,
                value=record.value_redacted,
                extracted=record.extracted,
                attempts=attempts,
                recoveries=record.recoveries_applied,
            )
            return _StepOutcome("ok", record)

    async def _attempt_step(
        self,
        capability: Capability,
        step: Step,
        inputs: dict[str, str],
        outputs: dict[str, Any],
        approved: bool,
        record: StepRecord,
    ) -> ReplayError | None:
        """Perform the step once. Returns an error, or None on success."""
        if step.pre_wait is not None:
            ok = await self.surface.wait_for(step.pre_wait.condition, step.pre_wait.timeout_ms)
            if not ok and step.action == "wait":
                return ReplayError(
                    **{"class": ErrorClass.TIMEOUT},
                    step_id=step.id,
                    step_intent=step.intent,
                    expected=step.pre_wait.condition.describe(),
                    observed=await self._describe_current_state(),
                    message=f"wait timed out after {step.pre_wait.timeout_ms}ms",
                )

        if step.action in ("wait", "assert"):
            return None

        value = substitute(step.value, inputs)
        url = substitute(step.url, inputs)

        # Resolve first so the policy gate can see the control's label — the
        # same text a human operator would read before clicking.
        resolved: Resolved | None = None
        if step.target is not None:
            res = await self.surface.resolve(step.target)
            if isinstance(res, Ambiguous):
                return ReplayError(
                    **{"class": ErrorClass.ELEMENT_AMBIGUOUS},
                    step_id=step.id,
                    step_intent=step.intent,
                    expected=f"exactly one match for {step.target.describe()}",
                    observed=f"{res.count} elements matched via {res.signal}",
                    message="ambiguous descriptor — refusing to guess which control to use",
                )
            if isinstance(res, NotFound):
                return ReplayError(
                    **{"class": ErrorClass.ELEMENT_NOT_FOUND},
                    step_id=step.id,
                    step_intent=step.intent,
                    expected=step.target.describe(),
                    observed=f"no match; tried {res.tried}",
                    message="element not found at any locator tier",
                )
            resolved = res
            record.locator_tier = res.tier
            record.locator_used = res.signal
            if res.tier > 0:
                # Drift signal. Primary failed, a fallback saved us — the run
                # succeeds but the artifact is quietly decaying.
                self.evidence.log(
                    "locator_fallback",
                    step=step.id,
                    tier=res.tier,
                    used=res.signal,
                    primary=step.target.primary.describe(),
                )

        label = (step.target.primary.name if step.target else None) or (
            step.target.describe() if step.target else None
        )
        decision = self.policy.check(
            step.action, url=url, label=label, approved=approved
        )
        if decision.verdict == "deny":
            return ReplayError(
                **{"class": ErrorClass.POLICY_DENIED},
                step_id=step.id,
                step_intent=step.intent,
                expected="action within policy",
                observed=decision.reason,
                message="blocked by policy",
            )
        if decision.verdict == "require_approval":
            return ReplayError(
                **{"class": ErrorClass.APPROVAL_REQUIRED},
                step_id=step.id,
                step_intent=step.intent,
                expected="human approval for an irreversible action",
                observed=decision.reason,
                message="approval required",
            )

        if step.action in ("type", "select") and value is not None:
            spec = next((p for p in capability.inputs if f"{{{{{p.name}}}}}" in (step.value or "")), None)
            record.value_redacted = (
                Redactor.for_logging(spec.name, value, spec.sensitivity)
                if spec
                else self.evidence.redactor.text(value)
            )

        act = await self.surface.act(
            step.action,
            resolved,
            value=value,
            url=url,
            key=step.key,
            kind=step.extract_kind,
            attribute=step.extract_attribute,
            timeout_ms=step.timeout_ms,
        )
        if not act.ok:
            return ReplayError(
                **{"class": ErrorClass.TIMEOUT},
                step_id=step.id,
                step_intent=step.intent,
                expected=f"{step.action} to succeed",
                observed=act.detail,
                message="action failed on the surface",
            )

        if step.action == "extract" and step.extract_as:
            outputs[step.extract_as] = act.extracted
            record.extracted = {step.extract_as: act.extracted}

        return None

    # -- detection helpers -------------------------------------------------

    async def _match_recovery(
        self, step: Step, error: ReplayError | None
    ) -> tuple[Recovery, str] | None:
        for rec in step.on_error:
            if await self.surface.check(rec.when):
                return rec, rec.when.describe()
        return None

    async def _match_outcome(self, capability: Capability) -> tuple[str, str | None] | None:
        for outcome in capability.outcomes:
            if await self.surface.check(outcome.detect):
                detail = None
                if outcome.detail_from is not None:
                    res = await self.surface.resolve(outcome.detail_from)
                    if isinstance(res, Resolved):
                        act = await self.surface.act("extract", res, kind="text")
                        detail = act.extracted
                return outcome.name, detail or outcome.description
        return None

    async def _outcome_detail(self, capability: Capability, name: str | None) -> str | None:
        outcome = next((o for o in capability.outcomes if o.name == name), None)
        if outcome is None:
            return None
        if outcome.detail_from is not None:
            res = await self.surface.resolve(outcome.detail_from)
            if isinstance(res, Resolved):
                act = await self.surface.act("extract", res, kind="text")
                if act.extracted:
                    return act.extracted
        return outcome.description

    async def _apply_recovery(self, rec: Recovery, step: Step) -> str:
        self.evidence.log("recovery", step=step.id, do=rec.do, when=rec.when.describe())

        if rec.do == "dismiss" and rec.target is not None:
            res = await self.surface.resolve(rec.target)
            if isinstance(res, Resolved):
                await self.surface.act("click", res)
            return "continue"

        if rec.do == "wait":
            # Wait for the triggering condition to clear, not for a fixed
            # duration. A transient spinner that resolves in 300ms should not
            # cost the full budget.
            await self.surface.wait_for(_invert(rec.when), rec.wait_ms)
            return "continue"

        if rec.do == "retry":
            return "continue"

        if rec.do == "reauth":
            if self.reauth is None:
                return "escalate"
            ok = await self.reauth()
            return "continue" if ok else "escalate"

        if rec.do == "return_outcome":
            return "business"

        if rec.do == "escalate":
            return "escalate"

        return "fail"

    async def _describe_current_state(self) -> str:
        try:
            obs = await self.surface.observe()
        except Exception:  # pragma: no cover - diagnostics must never mask the real error
            return "could not observe surface"
        head = " | ".join(
            e.render() for e in obs.elements[:6]
        )
        text = self.evidence.redactor.text(obs.text.strip().replace("\n", " ")[:200])
        return f"url={obs.url} title={obs.title!r} text={text!r} controls=[{head}]"

    async def _capture_failure_evidence(self, label: str) -> None:
        """The richer signal the brief asks for on failure."""
        try:
            shot = await self.surface.snapshot_evidence(f"FAIL_{label}")
            dom = await self.surface.dom_snapshot(self.evidence.dir / "dom.html")
            self.evidence.log("failure_evidence", screenshot=shot.get("screenshot"), dom=dom)
        except Exception as exc:  # pragma: no cover
            self.evidence.log("failure_evidence_error", error=str(exc))

    # -- escalation --------------------------------------------------------

    async def _escalate(
        self,
        capability: Capability,
        step: Step | None,
        *,
        kind: str,
        reason: str,
        records: list[StepRecord],
        started: float,
        inputs: dict[str, str],
        resume_from: Step | None = None,
        remaining: list[Step] | None = None,
        outputs: dict[str, Any] | None = None,
    ) -> ReplayResult:
        shot = await self.surface.snapshot_evidence(f"ESCALATE_{step.id if step else 'start'}")
        summary = await self._describe_current_state()

        redacted_inputs = {
            p.name: Redactor.for_logging(p.name, inputs.get(p.name, ""), p.sensitivity)
            for p in capability.inputs
            if p.name in inputs
        }

        req = self.queue.raise_request(
            kind=kind,
            reason=reason,
            source="replay",
            capability=capability.ref,
            run_id=self.evidence.run_id,
            step_id=step.id if step else None,
            step_intent=step.intent if step else None,
            session_id=self.lease.session_id,
            url=summary,
            screenshot=shot.get("screenshot"),
            observation_summary=summary,
            redacted_inputs=redacted_inputs,
        )
        self.evidence.log(
            "escalation_raised",
            intervention=req.id,
            kind=kind,
            reason=reason,
            step=step.id if step else None,
            mode=self.escalation_mode,
        )

        if self.escalation_mode == "return":
            # Production default: hand the caller a ticket, do not hold a worker.
            return self._finish(
                ReplayResult.needs_human(
                    capability=capability.ref,
                    run_id=self.evidence.run_id,
                    intervention_id=req.id,
                    reason=reason,
                    outputs=outputs or {},
                ),
                records,
                started,
            )

        # Interactive mode: park on the same live session and let a human drive.
        before = await self.surface.observe()
        resolution = await self.queue.wait_for_resolution(req.id, self.escalation_timeout_s)
        after = await self.surface.observe()

        operator = req.operator or "unknown"
        action = diff_observations(before, after, operator, req.reason)
        req.human_actions.append(action)
        self.evidence.log(
            "human_handoff",
            intervention=req.id,
            operator=operator,
            resolution=resolution,
            url_before=action.observed_url_before,
            url_after=action.observed_url_after,
            elements_added=action.elements_added,
            elements_removed=action.elements_removed,
        )

        if resolution is None:
            return self._finish(
                ReplayResult.needs_human(
                    capability=capability.ref,
                    run_id=self.evidence.run_id,
                    intervention_id=req.id,
                    reason=f"{reason} (no operator response within "
                    f"{self.escalation_timeout_s:.0f}s)",
                    outputs=outputs or {},
                ),
                records,
                started,
            )

        if resolution == "abort":
            return self._finish(
                ReplayResult.needs_human(
                    capability=capability.ref,
                    run_id=self.evidence.run_id,
                    intervention_id=req.id,
                    reason=f"{reason} (operator aborted)",
                    outputs=outputs or {},
                ),
                records,
                started,
            )

        # resume / completed_manually: continue the remaining steps on the same
        # session, now that the human has unblocked it.
        steps_left = remaining or []
        if resolution == "completed_manually" and steps_left:
            steps_left = steps_left[1:]

        self.lease.assert_automation()
        outs = dict(outputs or {})
        for nxt in steps_left:
            outcome = await self._run_step(capability, nxt, inputs, outs, approved=True)
            records.append(outcome.record)
            if outcome.kind == "business":
                return self._finish(
                    ReplayResult.business(
                        capability=capability.ref,
                        run_id=self.evidence.run_id,
                        outcome=outcome.outcome or "unknown",
                        detail=outcome.detail,
                        outputs=outs,
                    ),
                    records,
                    started,
                )
            if outcome.kind in ("fail", "escalate"):
                return self._finish(
                    ReplayResult.failure(
                        capability=capability.ref,
                        run_id=self.evidence.run_id,
                        error=outcome.error
                        or ReplayError(
                            **{"class": ErrorClass.INTERNAL},
                            step_id=nxt.id,
                            message=outcome.reason or "failed after human handoff",
                        ),
                    ),
                    records,
                    started,
                )

        if not await self.surface.wait_for(capability.checkpoint, 5_000):
            return self._finish(
                ReplayResult.failure(
                    capability=capability.ref,
                    run_id=self.evidence.run_id,
                    error=ReplayError(
                        **{"class": ErrorClass.CHECKPOINT_FAILED},
                        expected=capability.checkpoint.describe(),
                        observed=await self._describe_current_state(),
                        message="resumed after handoff but never reached the checkpoint",
                    ),
                ),
                records,
                started,
            )

        return self._finish(
            ReplayResult.success(
                capability=capability.ref, run_id=self.evidence.run_id, outputs=outs
            ),
            records,
            started,
        )

    # -- finishing ---------------------------------------------------------

    def _finish(
        self, result: ReplayResult, records: list[StepRecord], started: float
    ) -> ReplayResult:
        result.steps = records
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.evidence = Evidence(
            run_id=self.evidence.run_id,
            directory=str(self.evidence.dir),
            log_file=str(self.evidence.log_path.name),
            screenshots=[r.screenshot for r in records if r.screenshot],
        )
        self.evidence.log("replay_end", status=result.status, duration_ms=result.duration_ms)
        self.evidence.write_result(result)
        return result


def _invert(condition: Condition) -> Condition:
    """The negation of a condition, used by `do: wait` handlers."""
    flip = {
        "text_present": "text_absent",
        "text_absent": "text_present",
        "element_present": "element_absent",
        "element_absent": "element_present",
    }
    if condition.kind in flip:
        return condition.model_copy(update={"kind": flip[condition.kind]})
    return condition


def new_run_id(prefix: str = "replay") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}_{stamp}_{secrets.token_hex(3)}"
