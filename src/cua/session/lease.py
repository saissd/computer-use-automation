"""Who is allowed to touch the browser right now.

The brief asks for a human to take control of *the same live session* the
automation was using, and then hand it back. The hard part of that is not
showing someone a screen — it is making sure two actors never drive the same
session at once, and that each knows when it is not their turn.

A lease answers that. Exactly one holder at a time; the automation checks it
**before every single action** and stops if it does not hold it. That check is
mandatory rather than advisory, which is what makes "cede control" real: once
the lease moves to a human, the automation physically cannot act, even if a
step is queued and a timer fires.

Two details that matter in practice:

* **Leases expire.** An operator who takes control and then goes to lunch would
  otherwise wedge a run forever. On expiry the lease reverts to automation and
  the event is recorded, so the run either resumes or fails cleanly.

* **Handback is explicit and evidenced.** We snapshot the surface before
  handing over and after taking back, and diff them. That gives an objective
  record of what the human changed, independent of what they say they did —
  which is what an auditor will want, and what a future version of this system
  would learn the missing steps from.
"""

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

Owner = Literal["automation", "human"]

DEFAULT_HUMAN_LEASE_SECONDS = 900


class LeaseLost(RuntimeError):
    """Raised when automation tries to act without holding the lease."""


@dataclass
class LeaseEvent:
    at: datetime
    from_owner: Owner
    to_owner: Owner
    holder: str
    note: str = ""


@dataclass
class SessionLease:
    """Single-writer token for one live browser session."""

    session_id: str
    owner: Owner = "automation"
    lease_id: str = field(default_factory=lambda: secrets.token_hex(8))
    holder: str = "automation"
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    history: list[LeaseEvent] = field(default_factory=list)

    _changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    # -- queries -----------------------------------------------------------

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and datetime.now(timezone.utc) >= self.expires_at

    def held_by_automation(self) -> bool:
        if self.owner == "human" and self.expired:
            self._revert_on_expiry()
        return self.owner == "automation"

    def assert_automation(self) -> None:
        """Call before every automated action. Cheap, and the whole point."""
        if not self.held_by_automation():
            raise LeaseLost(
                f"session {self.session_id}: lease is held by {self.holder!r} "
                f"since {self.since.isoformat()}"
            )

    # -- transitions -------------------------------------------------------

    def _record(self, to_owner: Owner, holder: str, note: str) -> None:
        self.history.append(
            LeaseEvent(
                at=datetime.now(timezone.utc),
                from_owner=self.owner,
                to_owner=to_owner,
                holder=holder,
                note=note,
            )
        )
        self.owner = to_owner
        self.holder = holder
        self.lease_id = secrets.token_hex(8)
        self.since = datetime.now(timezone.utc)
        self._changed.set()
        self._changed.clear()

    def grant_to_human(
        self, operator: str, ttl_seconds: int = DEFAULT_HUMAN_LEASE_SECONDS, note: str = ""
    ) -> str:
        """Automation pauses; the operator becomes the only actor."""
        self._record("human", operator, note or "operator took control")
        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        return self.lease_id

    def return_to_automation(self, note: str = "") -> str:
        """Operator hands back; the run may resume on the same session."""
        self._record("automation", "automation", note or "control returned")
        self.expires_at = None
        return self.lease_id

    def _revert_on_expiry(self) -> None:
        self._record(
            "automation",
            "automation",
            f"human lease expired at {self.expires_at.isoformat() if self.expires_at else '?'}",
        )
        self.expires_at = None

    # -- coordination ------------------------------------------------------

    async def wait_for_automation(self, timeout_s: float | None = None) -> bool:
        """Block until automation holds the lease again (or the wait times out)."""
        loop_deadline = timeout_s
        while not self.held_by_automation():
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=min(1.0, loop_deadline or 1.0))
            except asyncio.TimeoutError:
                pass
            if loop_deadline is not None:
                loop_deadline -= 1.0
                if loop_deadline <= 0:
                    return self.held_by_automation()
        return True

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "owner": self.owner,
            "holder": self.holder,
            "lease_id": self.lease_id,
            "since": self.since.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "history": [
                {
                    "at": e.at.isoformat(),
                    "from": e.from_owner,
                    "to": e.to_owner,
                    "holder": e.holder,
                    "note": e.note,
                }
                for e in self.history
            ],
        }
