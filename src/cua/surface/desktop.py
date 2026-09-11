"""Desktop surface — an intentional, documented stub.

This is a **cut**, not an oversight (see REPORT.md § Cuts). It exists to keep
the seam honest: if the core abstraction were secretly web-shaped, writing
even this skeleton would be impossible, and the heterogeneity claim in the
write-up would be unfalsifiable hand-waving.

What a real implementation would do, and why the artifact schema already
accommodates it:

  observe()   Walk the UI Automation tree from the application's root element
              (`IUIAutomation::GetRootElement` -> `FindAll` with a raw view
              walker). Every node already carries exactly what
              `ObservedElement` needs: `ControlType` maps to `role`,
              `Name` maps to `name`, `IsEnabled` maps to `enabled`,
              `BoundingRectangle` maps to `bbox`. Window handles play the part
              `frame_path` plays on the web.

  resolve()   Build a UIA property condition from the descriptor's signals.
              `role_name` becomes
              `CreateAndCondition(ControlType == X, Name == Y)` — a direct
              translation, which is the whole point of choosing role+name as
              the primary locator. `label` maps to the LabeledBy property.
              `text_near` maps to sibling traversal in the tree. `css` has no
              analogue and is skipped, which is exactly the right behaviour:
              a descriptor that only has a CSS fallback is web-only, and the
              ranked list degrades rather than breaking.

  act()       Invoke/Toggle/Value control patterns rather than synthetic
              clicks: `IUIAutomationInvokePattern::Invoke` for buttons,
              `IUIAutomationValuePattern::SetValue` for text fields.

  check()     Same Condition primitives; `text_present` becomes a tree search
              over Name/Value properties instead of `document.body.innerText`.

The consequence worth stating: **a capability artifact recorded against a web
app is not automatically portable to a desktop app** — the entry point and the
specific control names differ. What *is* portable is the schema, the replay
engine, the error taxonomy, the policy gate and the escalation path. Only the
driver is rewritten, and `Surface` is the entire contract it must satisfy.
"""

from typing import Any

from cua.core.models import Condition, ElementDescriptor
from cua.surface.base import ActResult, Observation, Resolution

_MSG = (
    "DesktopSurface is a documented stub. The Surface protocol it would "
    "implement is defined in cua.surface.base; see this module's docstring "
    "for the UI Automation mapping and REPORT.md for why this was cut."
)


class DesktopSurface:
    """Placeholder implementation of the `Surface` protocol."""

    def __init__(self, app_path: str, **_: Any) -> None:
        self.app_path = app_path

    async def observe(self, screenshot: bool = False) -> Observation:
        raise NotImplementedError(_MSG)

    async def resolve(self, descriptor: ElementDescriptor) -> Resolution:
        raise NotImplementedError(_MSG)

    async def act(self, action: str, resolved: Any = None, **kwargs: Any) -> ActResult:
        raise NotImplementedError(_MSG)

    async def wait_for(self, condition: Condition, timeout_ms: int) -> bool:
        raise NotImplementedError(_MSG)

    async def check(self, condition: Condition) -> bool:
        raise NotImplementedError(_MSG)

    async def snapshot_evidence(self, label: str) -> dict[str, str]:
        raise NotImplementedError(_MSG)
