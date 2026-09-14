"""The surface abstraction — the seam between *perceiving/acting* and *the flow*.

Everything above this line (the discovery loop, the replay engine, the policy
gate, the artifact schema) is written against `Surface` and knows nothing about
Playwright, the DOM, or a browser. Everything below it is surface-specific.

That seam is the answer to the heterogeneity question in the brief. A legacy
web app and a Windows desktop app differ enormously in *how* you enumerate
controls and click them, and not at all in *what a recorded flow says*: "click
the button whose accessible name is Search". Because `ElementDescriptor` speaks
in roles and accessible names — the vocabulary shared by ARIA, Windows UIA and
macOS AX — the same artifact can in principle drive either. Only the driver
changes.

`DesktopSurface` in this package is an intentional stub. The interface it would
implement is real; the implementation is out of scope (see REPORT.md, Cuts).
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from cua.core.models import Condition, ElementDescriptor


@dataclass
class ObservedElement:
    """One control as the agent sees it.

    `ref` is a per-observation integer. The LLM acts on refs and never sees a
    selector, which means it structurally cannot invent one. The harness maps
    ref -> concrete element and captures a descriptor from it.
    """

    ref: int
    role: str
    name: str
    tag: str
    value: str | None = None
    enabled: bool = True
    frame_path: list[str] = field(default_factory=list)
    bbox: dict[str, float] | None = None
    attrs: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        """Compact one-line form for the model's context window."""
        if self.role == "cell":
            line = f"[{self.ref}] cell value='{self.value or ''}'"
            if self.attrs.get("row"):
                line += f" row='{self.attrs['row']}'"
            return line
        bits = [f"[{self.ref}]", self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.value:
            bits.append(f"value={self.value!r}")
        if not self.enabled:
            bits.append("(disabled)")
        if self.frame_path:
            bits.append(f"frame={'/'.join(self.frame_path)}")
        return " ".join(bits)


@dataclass
class FrameInfo:
    name: str
    url: str
    path: list[str]


@dataclass
class Observation:
    """A complete perception of the surface at one instant."""

    url: str
    title: str
    elements: list[ObservedElement]
    text: str
    frames: list[FrameInfo] = field(default_factory=list)
    screenshot: str | None = None
    state_hash: str = ""

    def by_ref(self, ref: int) -> ObservedElement | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def render(self, max_elements: int = 120) -> str:
        """The observation as the model receives it.

        Interactables as a numbered list, plus the visible text. Deliberately
        *not* HTML: the point of this design is that the agent reasons over the
        same semantic view a screen reader exposes, so that what it learns
        transfers to surfaces that have no HTML at all.
        """
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.frames:
            lines.append(
                "FRAMES: " + ", ".join(f"{f.name or '(unnamed)'}" for f in self.frames)
            )
        lines.append("")
        controls = [e for e in self.elements if e.role != "cell"]
        values = [e for e in self.elements if e.role == "cell"]
        for heading, group in (
            ("INTERACTABLE ELEMENTS:", controls),
            ("READABLE VALUES (extract only):", values),
        ):
            lines.append(heading)
            if not group:
                lines.append("  (none)")
            for el in group[:max_elements]:
                lines.append("  " + el.render())
            if len(group) > max_elements:
                lines.append(f"  ... {len(group) - max_elements} more omitted")
            lines.append("")
        lines.append("VISIBLE TEXT:")
        lines.append(self.text.strip()[:4000])
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Resolution outcomes — three states, because "ambiguous" must not be silent
# --------------------------------------------------------------------------


@dataclass
class Resolved:
    """Exactly one element matched."""

    handle: Any
    tier: int
    signal: str
    frame_path: list[str]


@dataclass
class Ambiguous:
    """More than one element matched and the descriptor did not say which.

    This is a hard failure, never a coin flip. Silently taking the first match
    is how automation ends up acting on the wrong row of a member's accounts.
    """

    count: int
    tier: int
    signal: str


@dataclass
class NotFound:
    """No element matched at any tier."""

    tried: list[str]


Resolution = Resolved | Ambiguous | NotFound


@dataclass
class ActResult:
    ok: bool
    detail: str = ""
    extracted: str | None = None


Action = Literal["click", "type", "select", "press", "extract", "navigate"]


@runtime_checkable
class Surface(Protocol):
    """What every surface driver must provide.

    Six methods. A desktop implementation would back `observe` with UIA's
    element tree, `resolve` with UIA property conditions, and `act` with
    UIA invoke/value patterns — and the artifact schema would not change.
    """

    async def observe(self, screenshot: bool = False) -> Observation: ...

    async def resolve(self, descriptor: ElementDescriptor) -> Resolution: ...

    async def act(
        self, action: Action, resolved: Resolved | None = None, **kwargs: Any
    ) -> ActResult: ...

    async def wait_for(self, condition: Condition, timeout_ms: int) -> bool: ...

    async def check(self, condition: Condition) -> bool: ...

    async def snapshot_evidence(self, label: str) -> dict[str, str]: ...
