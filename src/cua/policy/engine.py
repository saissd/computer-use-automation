"""The policy gate.

Enforced at the **action layer**, never in the prompt. A system prompt that
says "do not leave this domain" is a suggestion; a check that runs between the
model's decision and the browser call is a control. That distinction is also
the only real mitigation for prompt injection from page content: a malicious
page can talk the model into *deciding* anything, and none of it reaches the
surface unless this module agrees.

Three questions this answers:

  1. Is the target in scope?     (domain + route allowlist)
  2. Is the verb permitted?      (action allowlist)
  3. How dangerous is it?        (risk classification)

Risk handling is deliberately conservative and asymmetric. `read_only` runs
freely. `reversible_write` runs and is flagged. `irreversible` is never
executed unattended: it is blocked and routed to a human, even mid-replay,
even though that means an automated capability can stop half-finished. In a
regulated environment the cost of a wrong irreversible action is unbounded and
the cost of a stalled run is a phone call.
"""

import fnmatch
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from cua.core.models import RiskClass

Verdict = Literal["allow", "deny", "require_approval"]


@dataclass
class Decision:
    verdict: Verdict
    reason: str = ""
    risk: RiskClass = "read_only"

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


# Button/link text that signals an action you cannot take back. Deliberately
# broad: a false positive costs one human confirmation, a false negative could
# move money.
IRREVERSIBLE_HINTS = (
    "delete", "remove", "close account", "transfer", "wire", "submit payment",
    "disburse", "post transaction", "issue", "void", "charge off", "purge",
    "authorize", "send funds", "withdraw",
)

# Narrower than the irreversible list on purpose. Words like "new" and "apply"
# appear constantly on read-only controls ("New Search", "Apply Filter"), and a
# heuristic that cries wolf on every lookup trains people to ignore it. The
# authoritative signal for risk is `Capability.risk`, declared on the artifact
# and reviewed by a human; this list is only a backstop for discovery, where no
# declaration exists yet.
REVERSIBLE_WRITE_HINTS = (
    "create", "open ", "add ", "save", "update", "edit", "submit", "post ",
)


@dataclass
class PolicyConfig:
    """Loaded from config/policy.yaml. Everything is denied unless listed."""

    allowed_domains: list[str] = field(default_factory=list)
    allowed_routes: list[str] = field(default_factory=lambda: ["/*"])
    allowed_actions: list[str] = field(
        default_factory=lambda: ["click", "type", "select", "press", "extract", "navigate"]
    )
    denied_actions: list[str] = field(
        default_factory=lambda: ["download", "upload", "file_dialog", "execute_script"]
    )
    irreversible_hints: list[str] = field(default_factory=lambda: list(IRREVERSIBLE_HINTS))
    allow_irreversible: bool = False
    max_steps: int = 40
    max_seconds: int = 300

    @classmethod
    def load(cls, path: str | Path) -> "PolicyConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            # Fail loudly. A typo in a security control that silently does
            # nothing is worse than no control, because it reads as protection.
            raise ValueError(f"unknown policy keys in {path}: {sorted(unknown)}")
        return cls(**raw)


class PolicyEngine:
    def __init__(self, config: PolicyConfig):
        self.config = config

    # -- scope -------------------------------------------------------------

    def check_url(self, url: str) -> Decision:
        parsed = urlparse(url)
        host = parsed.netloc or ""
        if not host:
            return Decision("deny", f"unparseable url {url!r}")

        if not any(fnmatch.fnmatch(host, pat) for pat in self.config.allowed_domains):
            return Decision(
                "deny",
                f"host {host!r} is not in the allowlist "
                f"{self.config.allowed_domains}",
            )

        # Normalise before matching. `fnmatch`'s `*` happily spans `/`, so a
        # pattern like "/member/*" would otherwise admit
        # "/member/1/../../_control/inject" — the allowlist would appear to
        # hold while the request went somewhere else entirely.
        route = posixpath.normpath(parsed.path or "/")
        if not route.startswith("/"):
            return Decision("deny", f"route {parsed.path!r} escapes the root")
        if not any(fnmatch.fnmatch(route, pat) for pat in self.config.allowed_routes):
            return Decision(
                "deny", f"route {route!r} is not in the route allowlist"
            )

        return Decision("allow")

    # -- verbs -------------------------------------------------------------

    def check_action_type(self, action: str) -> Decision:
        if action in self.config.denied_actions:
            return Decision("deny", f"action type {action!r} is explicitly denied")
        if action not in self.config.allowed_actions:
            return Decision("deny", f"action type {action!r} is not in the allowlist")
        return Decision("allow")

    # -- risk --------------------------------------------------------------

    def classify(self, action: str, label: str | None = None) -> RiskClass:
        """Classify one concrete action.

        Signal is the accessible name of the control being acted on — which is
        exactly the text a human operator reads before deciding whether a
        button is scary. That is not a coincidence; it is why role+name is a
        good primary locator in the first place.
        """
        # Reading and moving around change nothing.
        if action in ("extract", "press", "navigate", "wait", "assert"):
            return "read_only"

        # Filling a field mutates form state but nothing persistent. It is the
        # subsequent click that commits, so that is where the real judgement is.
        if action in ("type", "select"):
            return "reversible_write"

        text = (label or "").lower()
        if any(h in text for h in self.config.irreversible_hints):
            return "irreversible"
        if any(h in text for h in REVERSIBLE_WRITE_HINTS):
            return "reversible_write"
        return "read_only"

    def check(
        self,
        action: str,
        *,
        url: str | None = None,
        label: str | None = None,
        approved: bool = False,
    ) -> Decision:
        """The single gate every action passes through."""
        verb = self.check_action_type(action)
        if not verb.allowed:
            return verb

        if url is not None:
            scope = self.check_url(url)
            if not scope.allowed:
                return scope

        risk = self.classify(action, label)

        if risk == "irreversible" and not approved and not self.config.allow_irreversible:
            return Decision(
                "require_approval",
                f"action {action!r} on {label!r} is classified irreversible "
                "and requires human approval",
                risk=risk,
            )

        return Decision("allow", risk=risk)
