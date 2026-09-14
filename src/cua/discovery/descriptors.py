"""Turning a clicked element into a replayable descriptor.

This is the hinge of the whole system. A discovery run is only worth anything
if what it records can be found again next month, on a slightly different
build, at a different institution. So the moment the model acts on an element,
we capture every signal that element offers and rank them by how well each
one survives change.

The ranking, best first:

1. **role + accessible name** — semantic, and the vocabulary is shared with
   Windows UIA and macOS AX, so it is the only tier with a desktop future.
   Survives CSS rewrites, framework upgrades and rebrands. It fails when a
   control has no name at all, which in legacy apps is common.
2. **label association** — a real `<label for>`. Nearly as good, web-specific.
3. **placeholder** — weaker, but stable in practice.
4. **text_near** — the label in the adjacent table cell. This is the tier that
   makes 1990s table-layout forms addressable at all. Fragile against layout
   changes, which is exactly why it is not first.
5. **css / id** — last. Generated ids like `ctl00_ContentPlaceHolder1_txtMbrNo`
   are stable *within* one vendor build and meaningless across versions and
   tenants, so they are a useful backstop and a terrible primary.

We deliberately never record coordinates. They are the highest-fidelity
description of where something was and the least useful description of what it
was, which is precisely why a screenshot-and-coordinates agent cannot produce
a durable artifact.
"""

from cua.core.models import CapturedEvidence, ElementDescriptor, LocatorSignal, Scope
from cua.surface.base import ObservedElement

# Accessible names this generic are not worth using as a primary signal.
WEAK_NAMES = {"", "submit", "button", "go", "ok", "...", "click here"}


def build_descriptor(
    element: ObservedElement, signals: dict, *, scope_to_frame: bool = True
) -> ElementDescriptor:
    """Build a ranked descriptor from one observed element and its raw signals."""
    tiers: list[LocatorSignal] = []

    name = (element.name or "").strip()
    if name.lower() not in WEAK_NAMES:
        tiers.append(
            LocatorSignal(by="role_name", role=element.role, name=name, exact=True)
        )

    label_for = (signals.get("labelFor") or "").strip()
    if label_for:
        tiers.append(LocatorSignal(by="label", text=label_for, exact=True))

    placeholder = (signals.get("placeholder") or "").strip()
    if placeholder:
        tiers.append(LocatorSignal(by="placeholder", text=placeholder, exact=True))

    # text_near is only sound when the target cell holds exactly one control;
    # otherwise the descriptor would be ambiguous by construction and replay
    # would (correctly) refuse to act on it.
    if signals.get("siblingsInCell", 0) <= 1:
        left = (signals.get("leftLabel") or "").strip()
        above = (signals.get("aboveLabel") or "").strip()
        if left:
            tiers.append(
                LocatorSignal(by="text_near", anchor=left, direction="right", offset=1)
            )
        elif above:
            tiers.append(
                LocatorSignal(by="text_near", anchor=above, direction="below", offset=1)
            )

    element_id = (signals.get("id") or "").strip()
    if element_id:
        tiers.append(LocatorSignal(by="css", selector=f"#{element_id}"))
    elif signals.get("nameAttr"):
        tiers.append(
            LocatorSignal(by="css", selector=f"[name='{signals['nameAttr']}']")
        )

    if not tiers:
        # Nothing semantic at all. Record a positional CSS path so the run is
        # not lost, but this is a descriptor a reviewer should be suspicious of.
        tiers.append(LocatorSignal(by="css", selector=signals.get("tag", "*")))

    return ElementDescriptor(
        primary=tiers[0],
        fallbacks=tiers[1:],
        scope=Scope(frame_path=element.frame_path) if scope_to_frame and element.frame_path else None,
        captured=CapturedEvidence(
            role=element.role,
            name=element.name,
            tag=element.tag,
            bbox=element.bbox,
            recorded_html_snippet=(signals.get("outerHTMLHead") or "")[:160] or None,
        ),
    )


def build_extraction_descriptor(
    element: ObservedElement, signals: dict
) -> ElementDescriptor:
    """Descriptor for a value being read rather than acted on.

    Read targets are usually static cells with no accessible name, so the
    adjacent-label signal carries more weight here than it does for controls.

    When the surface found a unique, digit-free label further left in the row
    (`rowAnchor`), that becomes the primary: the immediate neighbour of a value
    is frequently more data ("Active", an account number), and anchoring on
    data bakes this run's record into the artifact.
    """
    desc = build_descriptor(element, signals)

    anchor = (signals.get("rowAnchor") or "").strip()
    if anchor:
        near = LocatorSignal(
            by="text_near",
            anchor=anchor,
            direction="right",
            offset=int(signals.get("rowAnchorOffset") or 1),
        )
        others = [
            s for s in desc.signals()
            if s.by != "text_near" and not (s.by == "css" and s.selector == signals.get("tag"))
        ]
        inner_id = (signals.get("innerId") or "").strip()
        if inner_id and not signals.get("id"):
            others.append(LocatorSignal(by="css", selector=f"#{inner_id}"))
        return ElementDescriptor(
            primary=near, fallbacks=others, scope=desc.scope, captured=desc.captured
        )

    near = next((s for s in desc.signals() if s.by == "text_near"), None)
    if near is not None and desc.primary.by != "text_near":
        others = [s for s in desc.signals() if s is not near]
        return ElementDescriptor(
            primary=near,
            fallbacks=others,
            scope=desc.scope,
            captured=desc.captured,
        )
    return desc
