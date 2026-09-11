"""Escalation: detecting "stuck", routing it, and resuming afterwards.

An intervention request is the single object both producers emit — the
discovery loop when the model cannot make progress, and the replay engine when
it hits a condition it must not guess its way through. One type, two
producers, one console. That is why `needs_human` is a first-class
`ReplayResult` variant rather than an exception: escalation is an outcome the
caller has to handle, not an error it can swallow.

**Two escalation modes**, because the right behaviour differs by caller:

  return  The default, and what a production AI agent gets. We raise the
          request, persist the context, and return immediately with a ticket
          id. Nothing blocks. The calling agent decides whether to wait, tell
          the member "let me check with someone", or drop it.

  wait    For interactive and demo use. The run parks on the same live
          session, a human works in the browser, and the run resumes where it
          stopped. This is the mode the demo exercises.

Blocking a production worker for the length of a human's coffee break is the
kind of design that looks fine in a demo and falls over at 200 concurrent
sessions, so the blocking mode is opt-in.
"""

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

InterventionKind = Literal[
    "no_progress",           # observation unchanged across N actions
    "repeated_action",       # same action twice with no effect
    "model_requested",       # the model called escalate()
    "policy_block",          # a required action is outside the allowlist
    "approval_required",     # irreversible action needs a person to sign off
    "unrecoverable_error",   # replay hit a condition with no declared handler
    "session_expired",       # re-auth failed or was not possible
    "budget_exhausted",      # step or time budget hit
]

Resolution = Literal["resume", "abort", "completed_manually"]


class HumanAction(BaseModel):
    """What the operator reported doing, plus what we observed independently."""

    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    operator: str
    note: str = ""
    observed_url_before: str | None = None
    observed_url_after: str | None = None
    elements_added: list[str] = Field(default_factory=list)
    elements_removed: list[str] = Field(default_factory=list)
    text_delta_chars: int = 0


class InterventionRequest(BaseModel):
    """Everything a human needs to act, and nothing they should not see."""

    id: str = Field(default_factory=lambda: "int_" + secrets.token_hex(6))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    state: Literal["open", "taken", "resolved"] = "open"

    kind: InterventionKind
    reason: str

    # Context — enough to act on, already redacted.
    source: Literal["discovery", "replay"]
    capability: str | None = None
    goal: str | None = None
    run_id: str | None = None
    step_id: str | None = None
    step_intent: str | None = None
    session_id: str | None = None

    url: str | None = None
    screenshot: str | None = None
    observation_summary: str | None = None
    redacted_inputs: dict[str, Any] = Field(default_factory=dict)

    # Outcome
    operator: str | None = None
    taken_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: Resolution | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)

    def brief(self) -> str:
        where = f" at step {self.step_id} ({self.step_intent})" if self.step_id else ""
        return f"[{self.kind}] {self.capability or self.goal}{where}: {self.reason}"


class InterventionQueue:
    """In-process queue. One institution's worth of interventions.

    Deliberately not a database or a message broker. The brief explicitly
    warns against building scaling infrastructure, and the interesting design
    here is the *state machine and its context*, not the transport. Swapping
    this for a real queue is a change to one class.
    """

    def __init__(self) -> None:
        self._items: dict[str, InterventionRequest] = {}
        self._events: dict[str, asyncio.Event] = {}

    def raise_request(self, **kwargs: Any) -> InterventionRequest:
        req = InterventionRequest(**kwargs)
        self._items[req.id] = req
        self._events[req.id] = asyncio.Event()
        return req

    def get(self, intervention_id: str) -> InterventionRequest | None:
        return self._items.get(intervention_id)

    def list(self, *, open_only: bool = False) -> list[InterventionRequest]:
        items = list(self._items.values())
        if open_only:
            items = [i for i in items if i.state != "resolved"]
        return sorted(items, key=lambda i: i.created_at, reverse=True)

    def take(self, intervention_id: str, operator: str) -> InterventionRequest | None:
        req = self._items.get(intervention_id)
        if req is None or req.state == "resolved":
            return None
        req.state = "taken"
        req.operator = operator
        req.taken_at = datetime.now(timezone.utc)
        return req

    def resolve(
        self,
        intervention_id: str,
        resolution: Resolution,
        human_action: HumanAction | None = None,
    ) -> InterventionRequest | None:
        req = self._items.get(intervention_id)
        if req is None:
            return None
        req.state = "resolved"
        req.resolution = resolution
        req.resolved_at = datetime.now(timezone.utc)
        if human_action:
            req.human_actions.append(human_action)
        ev = self._events.get(intervention_id)
        if ev:
            ev.set()
        return req

    async def wait_for_resolution(
        self, intervention_id: str, timeout_s: float = 600.0
    ) -> Resolution | None:
        """Park until an operator resolves this request.

        Only used in `wait` escalation mode. Returns None on timeout, which the
        caller must treat as a failure rather than as approval — silence is
        never consent for an irreversible action.
        """
        ev = self._events.get(intervention_id)
        if ev is None:
            return None
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
        req = self._items.get(intervention_id)
        return req.resolution if req else None


# One process, one queue. The operator console and the running agent are the
# same process precisely so that "the same live session" is literally true.
QUEUE = InterventionQueue()


def diff_observations(before, after, operator: str, note: str) -> HumanAction:
    """Objective record of what changed while a human held the lease.

    Independent of the operator's own account of what they did. An auditor
    gets a fact, not a self-report, and a future version of this system could
    use the same diff to propose the steps that were missing from the
    capability.
    """
    before_els = {f"{e.role}:{e.name}" for e in before.elements}
    after_els = {f"{e.role}:{e.name}" for e in after.elements}
    return HumanAction(
        operator=operator,
        note=note,
        observed_url_before=before.url,
        observed_url_after=after.url,
        elements_added=sorted(after_els - before_els)[:40],
        elements_removed=sorted(before_els - after_els)[:40],
        text_delta_chars=len(after.text) - len(before.text),
    )
