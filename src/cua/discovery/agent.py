"""The LLM-driven discovery loop: observe -> decide -> act.

This is the only part of the system that calls a model, and it runs exactly
once per capability. Everything it produces is consumed by the deterministic
replay engine, which never calls a model at all.

The load-bearing decision here is that **the model does not write the
artifact**. Steps are accreted mechanically: when the model acts on control
`[12]`, the harness looks up the real element behind that ref, extracts every
identifying signal it has, ranks them, and appends a typed `Step`. The model
contributes the *intent sentence* and the *sequence*; it never contributes a
locator. Asking a model to reconstruct a fourteen-step flow from memory at the
end of a run produces something that reads beautifully and replays badly.

Stuck detection is explicit rather than emergent. Five signals, any of which
ends the run and raises an intervention with enough context for a human to
pick it up.
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cua.core.models import (
    Capability,
    Condition,
    Provenance,
    Recovery,
    Step,
    Target,
)
from cua.discovery.descriptors import build_descriptor, build_extraction_descriptor
from cua.discovery.prompts import SYSTEM_PROMPT, TOOLS, goal_message
from cua.evidence.writer import EvidenceWriter
from cua.policy.engine import PolicyEngine
from cua.session.intervention import QUEUE, InterventionQueue
from cua.session.lease import SessionLease
from cua.surface.base import Observation
from cua.surface.web import WebSurface

DEFAULT_MODEL = "claude-opus-5"

# Stuck thresholds. Tuned to stop early: a discovery run that has spun three
# times is not about to have an insight, and every extra turn is spend.
NO_PROGRESS_LIMIT = 3
REPEAT_ACTION_LIMIT = 2


@dataclass
class DiscoveryResult:
    status: Literal["success", "escalated", "exhausted", "failed"]
    run_id: str
    reason: str = ""
    capability: Capability | None = None
    intervention_id: str | None = None
    steps_recorded: int = 0
    turns: int = 0
    raw_outputs: dict[str, str] = field(default_factory=dict)
    typed_values: dict[str, str] = field(default_factory=dict)
    success_text: str | None = None
    summary: str = ""


class DiscoveryAgent:
    def __init__(
        self,
        surface: WebSurface,
        policy: PolicyEngine,
        lease: SessionLease,
        evidence: EvidenceWriter,
        *,
        client: Any,
        model: str = DEFAULT_MODEL,
        queue: InterventionQueue | None = None,
        recovery_handlers: list[Recovery] | None = None,
        max_steps: int | None = None,
        max_seconds: int | None = None,
    ):
        self.surface = surface
        self.policy = policy
        self.lease = lease
        self.evidence = evidence
        # Nothing is classified yet during discovery — sensitivity is assigned
        # by generalisation, afterwards — so every number and amount is treated
        # as sensitive on the way to disk. Set here, not by the caller, so no
        # caller can forget it.
        self.evidence.redactor.strict = True
        self.client = client
        self.model = model
        self.queue = queue or QUEUE
        self.recovery_handlers = recovery_handlers or []
        self.max_steps = max_steps or policy.config.max_steps
        self.max_seconds = max_seconds or policy.config.max_seconds

    # ----------------------------------------------------------------------

    async def run(self, goal: str, entry: str, app_id: str) -> DiscoveryResult:
        run_id = self.evidence.run_id
        started = time.monotonic()

        self.evidence.log(
            "discovery_start", goal=goal, entry=entry, model=self.model,
            max_steps=self.max_steps,
        )

        gate = self.policy.check_url(entry)
        if not gate.allowed:
            return DiscoveryResult("failed", run_id, reason=f"entry point blocked: {gate.reason}")

        await self.surface.act("navigate", None, url=entry)
        obs = await self.surface.observe(screenshot=True)

        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    goal_message(goal, entry, self.policy.config.allowed_domains)
                    + "\n\n"
                    + obs.render()
                ),
            }
        ]

        steps: list[Step] = []
        outputs: dict[str, str] = {}
        typed: dict[str, str] = {}
        recent_hashes: list[str] = [obs.state_hash]
        recent_actions: list[str] = []
        turns = 0

        while True:
            turns += 1

            # --- stopping conditions -------------------------------------
            if len(steps) >= self.max_steps:
                return await self._escalate(
                    "budget_exhausted",
                    f"step budget of {self.max_steps} exhausted without reaching the goal",
                    goal, obs, run_id, steps, turns,
                )
            if time.monotonic() - started > self.max_seconds:
                return await self._escalate(
                    "budget_exhausted",
                    f"time budget of {self.max_seconds}s exhausted",
                    goal, obs, run_id, steps, turns,
                )
            if len(recent_hashes) >= NO_PROGRESS_LIMIT and len(
                set(recent_hashes[-NO_PROGRESS_LIMIT:])
            ) == 1 and len(steps) >= NO_PROGRESS_LIMIT:
                return await self._escalate(
                    "no_progress",
                    f"the screen has not changed across {NO_PROGRESS_LIMIT} actions",
                    goal, obs, run_id, steps, turns,
                )
            if len(recent_actions) >= REPEAT_ACTION_LIMIT and len(
                set(recent_actions[-REPEAT_ACTION_LIMIT:])
            ) == 1:
                return await self._escalate(
                    "repeated_action",
                    f"the same action was taken {REPEAT_ACTION_LIMIT} times with no effect",
                    goal, obs, run_id, steps, turns,
                )

            # --- decide ---------------------------------------------------
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16_000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                messages=messages,
            )

            # Thinking blocks must be replayed back unchanged, so append the
            # whole content list rather than extracting the text.
            messages.append({"role": "assistant", "content": response.content})

            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                said = " ".join(b.text for b in response.content if b.type == "text")
                self.evidence.log("model_no_tool_call", text=said[:500])
                messages.append(
                    {
                        "role": "user",
                        "content": "You did not call a tool. Call exactly one tool "
                        "now, or call escalate if you cannot proceed.\n\n"
                        + (await self.surface.observe()).render(),
                    }
                )
                if turns > 2 and not steps:
                    return DiscoveryResult(
                        "failed", run_id, reason="model never called a tool", turns=turns
                    )
                continue

            call = calls[0]
            name, args = call.name, call.input
            for literal in (args.get("text"), args.get("option")):
                self.evidence.redactor.add_value(literal)
            self.evidence.log(
                "model_decision",
                turn=turns,
                tool=name,
                args=self.evidence.redactor.obj(dict(args)),
                intent=args.get("intent"),
            )

            # --- terminal tools ------------------------------------------
            if name == "escalate":
                return await self._escalate(
                    "model_requested", args["reason"], goal, obs, run_id, steps, turns
                )

            if name == "done":
                success_text = self._safe_success_text(args.get("success_text", ""), typed, obs)
                self.evidence.log(
                    "discovery_done", summary=args.get("summary"), checkpoint=success_text
                )
                return DiscoveryResult(
                    "success",
                    run_id,
                    capability=self._assemble(
                        goal, entry, app_id, steps, outputs, success_text, run_id
                    ),
                    steps_recorded=len(steps),
                    turns=turns,
                    raw_outputs=outputs,
                    typed_values=typed,
                    success_text=success_text,
                    summary=args.get("summary", ""),
                )

            # --- act -----------------------------------------------------
            before = obs
            step, feedback, ok = await self._execute(name, args, obs, outputs, typed)

            if step is not None:
                steps.append(step)
                recent_actions.append(f"{name}:{args.get('ref')}:{args.get('text','')}")

            obs = await self._observe_after(
                before, expect_change=step is not None and name in ("click", "press", "select")
            )
            recent_hashes.append(obs.state_hash)

            if step is not None and ok:
                step.post_assert = self._infer_post_assert(before, obs, typed)

            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": f"{feedback}\n\n{obs.render()}",
                            **({"is_error": True} if not ok else {}),
                        }
                    ],
                }
            )

    # ----------------------------------------------------------------------

    async def _observe_after(self, before: Observation, *, expect_change: bool) -> Observation:
        """Observe once an action's effect has had a chance to land.

        A submit inside a frameset can return from click() before the frame
        has even started navigating, so waiting on load state sees the old,
        fully loaded document. Observing then hands the model a stale page and
        makes the step look like it did nothing — no post-assertion, and a
        false no-progress signal. So for actions that can navigate, poll
        briefly for the state to move. A click that genuinely does nothing
        still costs two seconds, not a stall.
        """
        if expect_change:
            for _ in range(10):
                probe = await self.surface.observe()
                if probe.state_hash != before.state_hash:
                    break
                await asyncio.sleep(0.2)
        return await self.surface.observe(screenshot=True)

    async def _execute(
        self,
        name: str,
        args: dict,
        obs: Observation,
        outputs: dict[str, str],
        typed: dict[str, str],
    ) -> tuple[Step | None, str, bool]:
        """Run one model-chosen action through the policy gate onto the surface."""
        self.lease.assert_automation()

        if name == "press":
            decision = self.policy.check("press")
            if not decision.allowed:
                return None, f"BLOCKED BY POLICY: {decision.reason}", False
            await self.surface.act("press", None, key=args["key"])
            return (
                Step(
                    id=f"s{len(outputs) + 1}_press",
                    intent=args["intent"],
                    action="press",
                    key=args["key"],
                    on_error=list(self.recovery_handlers),
                ),
                f"Pressed {args['key']}.",
                True,
            )

        ref = args.get("ref")
        element = obs.by_ref(ref) if ref is not None else None
        if element is None:
            return None, (
                f"There is no control numbered {ref} in the current observation. "
                "Re-read the list and use a number that is present."
            ), False

        label = element.name or element.tag
        if element.role == "cell" and name != "extract":
            return None, (
                f"[{ref}] is a readable value, not a control. Use extract to read it, "
                "or choose a numbered control to act on."
            ), False
        action ={"click": "click", "type": "type", "select": "select", "extract": "extract"}[name]

        decision = self.policy.check(action, label=label)
        if decision.verdict == "deny":
            self.evidence.log("policy_denied", action=action, label=label, reason=decision.reason)
            return None, f"BLOCKED BY POLICY: {decision.reason}", False
        if decision.verdict == "require_approval":
            self.evidence.log("policy_approval_required", action=action, label=label)
            return None, (
                f"BLOCKED: {decision.reason}. This action is irreversible and cannot "
                "be taken during discovery. If the goal requires it, call escalate."
            ), False

        signals = await self.surface.raw_signals(element)

        if name == "extract":
            descriptor = build_extraction_descriptor(element, signals)
        else:
            descriptor = build_descriptor(element, signals)

        # Verify the descriptor we just built actually resolves to this element
        # and nothing else. A descriptor that is ambiguous at record time would
        # be a landmine at replay time, so we catch it here where we can still
        # do something about it.
        res = await self.surface.resolve(descriptor)
        from cua.surface.base import Ambiguous, NotFound, Resolved

        if isinstance(res, Ambiguous):
            self.evidence.log(
                "descriptor_ambiguous", label=label, signal=res.signal, count=res.count
            )
            return None, (
                f"That control could not be described unambiguously "
                f"({res.count} controls match). Choose a control with a more "
                "distinctive name, or a different route to the same outcome."
            ), False
        if isinstance(res, NotFound):
            return None, "That control could not be re-resolved. Re-read the list.", False

        value = args.get("text") or args.get("option")
        act = await self.surface.act(
            action, res, value=value, kind="text", timeout_ms=10_000
        )
        if not act.ok:
            return None, f"The action failed: {act.detail}", False

        slug_source = args.get("output_name") if action == "extract" else label
        step_id = f"s{len(typed) + len(outputs) + 1}_{action}_{_slug(slug_source)}"[:48]
        step = Step(
            id=step_id,
            intent=args["intent"],
            action=action,  # type: ignore[arg-type]
            target=descriptor,
            value=value,
            extract_as=args.get("output_name") if action == "extract" else None,
            extract_kind="text",
            on_error=list(self.recovery_handlers),
            timeout_ms=10_000,
            retries=1,
        )

        if action == "extract":
            self.evidence.redactor.add_value(act.extracted)
            outputs[args["output_name"]] = act.extracted or ""
            return step, f"Extracted {args['output_name']} = {act.extracted!r}", True

        if value is not None:
            typed[step_id] = value

        return step, f"Done: {act.detail}.", True

    # ----------------------------------------------------------------------

    @staticmethod
    def _infer_post_assert(
        before: Observation, after: Observation, typed: dict[str, str]
    ) -> Condition | None:
        """Derive a verification for a step from what actually changed.

        Mechanical, not model-generated, because a checkpoint the model
        invented is a checkpoint nobody validated. We look for a distinctive
        phrase that appeared as a result of this step, and we exclude anything
        containing a value we typed — otherwise the assertion would encode this
        run's data and fail for every other input.
        """
        if before.state_hash == after.state_hash:
            return None

        old_lines = {ln.strip() for ln in before.text.splitlines()}
        typed_values = {v for v in typed.values() if v}

        candidates: list[str] = []
        for raw in after.text.splitlines():
            line = raw.strip()
            if not (8 <= len(line) <= 60) or line in old_lines:
                continue
            if any(v and v in line for v in typed_values):
                continue
            if not line[0].isupper():
                continue

            # A tab in innerText means a table row, and a table row on a
            # detail screen is record data — a member's branch, a balance, an
            # opening date. Asserting on it would bake this member into the
            # artifact and break it for every other input.
            if "	" in raw:
                continue
            # Chrome is words; data has digits. Requiring zero digits is
            # blunt, and that is the point: a checkpoint is worth nothing if
            # it is not true for every valid invocation.
            if any(c.isdigit() for c in line):
                continue
            candidates.append(line)

        if not candidates:
            return None
        best = max(candidates, key=len)
        return Condition(kind="text_present", text=best)

    @staticmethod
    def _safe_success_text(proposed: str, typed: dict[str, str], obs: Observation) -> str:
        """Reject a checkpoint that bakes this run's data into the artifact."""
        proposed = (proposed or "").strip()
        typed_values = {v for v in typed.values() if v}
        bad = any(v and v in proposed for v in typed_values)
        if proposed and not bad and proposed in obs.text:
            return proposed
        derived = DiscoveryAgent._infer_post_assert(
            Observation(url="", title="", elements=[], text=""), obs, typed
        )
        return derived.text if derived and derived.text else (proposed or obs.title)

    # ----------------------------------------------------------------------

    def _assemble(
        self,
        goal: str,
        entry: str,
        app_id: str,
        steps: list[Step],
        outputs: dict[str, str],
        success_text: str,
        run_id: str,
    ) -> Capability:
        """Build the draft capability from mechanically accreted steps.

        Parameterisation, naming and business outcomes are filled in by the
        generalisation pass (cua.discovery.generalize). What comes out of here
        is already valid and already replayable — just not yet reusable.
        """
        first = Step(
            id="s0_open",
            intent="Open the application at its entry point",
            action="navigate",
            url=entry,
            on_error=list(self.recovery_handlers),
            timeout_ms=15_000,
            retries=1,
        )
        return Capability(
            id="discovered_capability",
            name=goal[:80],
            version="0.1.0",
            status="draft",
            description=goal,
            target=Target(app_id=app_id, surface="legacy_web", entry=entry),
            steps=[first, *steps],
            checkpoint=Condition(kind="text_present", text=success_text),
            provenance=Provenance(
                discovered_by="llm",
                model=self.model,
                goal=goal,
                run_id=run_id,
                evidence_ref=str(Path(self.evidence.dir).name),
            ),
        )

    async def _escalate(
        self,
        kind: str,
        reason: str,
        goal: str,
        obs: Observation,
        run_id: str,
        steps: list[Step],
        turns: int,
    ) -> DiscoveryResult:
        shot = await self.surface.snapshot_evidence(f"ESCALATE_{kind}")
        req = self.queue.raise_request(
            kind=kind,
            reason=reason,
            source="discovery",
            goal=goal,
            run_id=run_id,
            session_id=self.lease.session_id,
            url=obs.url,
            screenshot=shot.get("screenshot"),
            observation_summary=self.evidence.redactor.text(obs.render(max_elements=25)),
            step_intent=steps[-1].intent if steps else None,
        )
        self.evidence.log("discovery_escalated", intervention=req.id, kind=kind, reason=reason)
        return DiscoveryResult(
            "escalated",
            run_id,
            reason=reason,
            intervention_id=req.id,
            steps_recorded=len(steps),
            turns=turns,
        )


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in (text or "x"))
    return "_".join(p for p in out.split("_") if p) or "x"
