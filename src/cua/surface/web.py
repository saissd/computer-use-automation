"""Playwright-backed web surface.

Two decisions worth explaining.

**We resolve to concrete element handles, not to Playwright Locators.**
A Locator is lazy and re-queries on use, which is usually a feature. Here it
hides the one fact we most need to assert on: *how many things matched*.
Resolving eagerly to a list of handles makes ambiguity an observable, and lets
us refuse to act rather than silently take the first match. Auto-waiting is
not lost — it moves into the explicit `WaitSpec` on each step, where it is
declared in the artifact and therefore reviewable.

**Frames are addressed by path, not flattened.** A legacy frameset app puts
the real content in a named child frame, and the same accessible name can
legitimately exist in two frames at once. `Scope.frame_path` keeps that
explicit in the artifact.
"""

import hashlib
import re
from pathlib import Path
from typing import Any

from playwright.async_api import ElementHandle, Error as PWError, Frame, Page

from cua.core.models import (
    CapturedEvidence,
    Condition,
    ElementDescriptor,
    LocatorSignal,
    Scope,
)
from cua.surface.base import (
    ActResult,
    Ambiguous,
    FrameInfo,
    NotFound,
    ObservedElement,
    Observation,
    Resolution,
    Resolved,
)
from cua.surface.js import DESCRIBE_JS, NEAR_JS, ROW_FILTER_JS, SNAPSHOT_JS

INTERACTABLE_TAGS = {"input", "select", "textarea", "button", "a"}


class WebSurface:
    """Drives a browser page as one `Surface`."""

    def __init__(self, page: Page, evidence_dir: Path | None = None):
        self.page = page
        self.evidence_dir = evidence_dir
        self._shot_seq = 0

    # -- perception --------------------------------------------------------

    def _frame_path(self, frame: Frame) -> list[str]:
        path: list[str] = []
        cur: Frame | None = frame
        while cur is not None and cur.parent_frame is not None:
            path.append(cur.name or "(unnamed)")
            cur = cur.parent_frame
        return list(reversed(path))

    async def _settle(self, frame: Frame, timeout_ms: int = 5_000) -> None:
        try:
            await frame.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except PWError:
            pass

    async def observe(self, screenshot: bool = False) -> Observation:
        elements: list[ObservedElement] = []
        texts: list[str] = []
        frames: list[FrameInfo] = []
        ref_base = 0

        # A click that submits a form inside a frameset leaves that frame
        # mid-navigation. Snapshotting now would yield a page with the content
        # frame missing, and the model would reason over it as if it were
        # real. Replay does not need this — its waits are declared per step.
        for frame in self.page.frames:
            await self._settle(frame)

        for frame in self.page.frames:
            path = self._frame_path(frame)
            try:
                snap = await frame.evaluate(SNAPSHOT_JS)
            except PWError:
                # The frame navigated out from under us mid-snapshot. Give the
                # new document one chance to land before skipping it.
                await self._settle(frame)
                try:
                    snap = await frame.evaluate(SNAPSHOT_JS)
                except PWError:
                    continue

            frames.append(FrameInfo(name=frame.name or "", url=frame.url, path=path))
            if snap.get("text"):
                texts.append(snap["text"])

            for raw in snap["elements"]:
                elements.append(
                    ObservedElement(
                        ref=ref_base + raw["ref"],
                        role=raw["role"],
                        name=raw["name"],
                        tag=raw["tag"],
                        value=raw["value"],
                        enabled=raw["enabled"],
                        frame_path=path,
                        bbox=raw["bbox"],
                        attrs=raw["attrs"],
                    )
                )
            ref_base += 10_000  # keep refs unique and frame-attributable

        text = "\n".join(texts)
        shot = await self.snapshot_evidence("observe") if screenshot else None

        obs = Observation(
            url=self.page.url,
            title=await self.page.title(),
            elements=elements,
            text=text,
            frames=frames,
            screenshot=(shot or {}).get("screenshot"),
        )
        obs.state_hash = self._hash(obs)
        return obs

    @staticmethod
    def _hash(obs: Observation) -> str:
        """Fingerprint of the perceived state.

        Used for stuck detection: if three actions in a row leave this
        unchanged, the agent is spinning and we escalate rather than burn the
        step budget.
        """
        sig = "|".join(
            [obs.url, obs.title]
            + sorted(f"{e.role}:{e.name}:{e.value or ''}" for e in obs.elements)
        )
        return hashlib.sha256(sig.encode("utf-8", "replace")).hexdigest()[:16]

    # -- element resolution ------------------------------------------------

    def _frames_in_scope(self, scope: Scope | None) -> list[Frame]:
        if scope is None or not scope.frame_path:
            return list(self.page.frames)
        wanted = scope.frame_path
        out = [f for f in self.page.frames if self._frame_path(f) == wanted]
        if out:
            return out
        # Named frame missing (app changed, or we are on an error page that
        # broke out of the frameset). Fall back to every frame rather than
        # failing here — the caller's assertions will catch a genuine problem,
        # and this keeps a frame rename from looking like a missing element.
        return list(self.page.frames)

    async def _find(self, frame: Frame, sig: LocatorSignal) -> list[ElementHandle]:
        try:
            if sig.by == "role_name":
                loc = frame.get_by_role(
                    sig.role,  # type: ignore[arg-type]
                    name=sig.name if sig.name else None,
                    exact=sig.exact,
                )
            elif sig.by == "label":
                loc = frame.get_by_label(sig.text, exact=sig.exact)
            elif sig.by == "placeholder":
                loc = frame.get_by_placeholder(sig.text, exact=sig.exact)
            elif sig.by == "text_exact":
                loc = frame.get_by_text(sig.text, exact=True)
            elif sig.by == "css":
                loc = frame.locator(sig.selector)
            elif sig.by == "text_near":
                count = await frame.evaluate(
                    NEAR_JS,
                    {
                        "anchor": sig.anchor,
                        "direction": sig.direction or "right",
                        "offset": sig.offset,
                    },
                )
                if not count:
                    return []
                loc = frame.locator("[data-cua-near]")
            else:  # pragma: no cover - exhaustive over LocatorStrategy
                return []
            return await loc.element_handles()
        except PWError:
            return []

    async def _apply_disambiguation(
        self, frame: Frame, handles: list[ElementHandle], descriptor: ElementDescriptor
    ) -> list[ElementHandle]:
        dis = descriptor.disambiguation
        if dis is None:
            return handles

        if dis.within_row_matching:
            # Only meaningful for candidates we marked ourselves.
            try:
                kept = await frame.evaluate(
                    ROW_FILTER_JS, {"text": dis.within_row_matching}
                )
                if kept:
                    handles = await frame.locator("[data-cua-near]").element_handles()
                else:
                    handles = [
                        h
                        for h in handles
                        if await self._in_row_containing(h, dis.within_row_matching)
                    ]
            except PWError:
                pass

        if dis.nth is not None:
            handles = handles[dis.nth : dis.nth + 1]

        return handles

    @staticmethod
    async def _in_row_containing(handle: ElementHandle, text: str) -> bool:
        try:
            return bool(
                await handle.evaluate(
                    "(el, t) => { const r = el.closest('tr');"
                    " return !!r && r.innerText.replace(/\\s+/g,' ').includes(t); }",
                    text,
                )
            )
        except PWError:
            return False

    async def resolve(self, descriptor: ElementDescriptor) -> Resolution:
        """Try each signal in rank order. Unique match or nothing."""
        tried: list[str] = []
        frames = self._frames_in_scope(descriptor.scope)

        for tier, sig in enumerate(descriptor.signals()):
            tried.append(f"tier{tier}:{sig.describe()}")
            for frame in frames:
                handles = await self._find(frame, sig)
                if not handles:
                    continue

                handles = await self._apply_disambiguation(frame, handles, descriptor)
                if not handles:
                    continue

                if len(handles) > 1:
                    # Never guess. An ambiguous descriptor is a defect in the
                    # artifact, and acting on the first match is how you end up
                    # transacting against the wrong account.
                    return Ambiguous(
                        count=len(handles), tier=tier, signal=sig.describe()
                    )

                return Resolved(
                    handle=handles[0],
                    tier=tier,
                    signal=sig.describe(),
                    frame_path=self._frame_path(frame),
                )

        return NotFound(tried=tried)

    @staticmethod
    async def _coerce_interactable(handle: ElementHandle) -> ElementHandle:
        """Descend from a container to the control inside it.

        `text_near` resolves to a table cell; the thing you actually type into
        is the <input> that cell wraps. Rather than making every descriptor
        encode that, we descend once here.
        """
        try:
            tag = (await handle.evaluate("el => el.tagName.toLowerCase()")) or ""
            if tag in INTERACTABLE_TAGS:
                return handle
            inner = await handle.query_selector("input, select, textarea, button, a")
            return inner or handle
        except PWError:
            return handle

    # -- action ------------------------------------------------------------

    async def act(
        self, action: str, resolved: Resolved | None = None, **kwargs: Any
    ) -> ActResult:
        try:
            if action == "navigate":
                await self.page.goto(kwargs["url"], wait_until="domcontentloaded")
                return ActResult(ok=True, detail=f"navigated to {kwargs['url']}")

            if action == "press":
                await self.page.keyboard.press(kwargs["key"])
                return ActResult(ok=True, detail=f"pressed {kwargs['key']}")

            if resolved is None:
                return ActResult(ok=False, detail=f"action {action!r} needs an element")

            handle = await self._coerce_interactable(resolved.handle)

            if action == "click":
                await handle.scroll_into_view_if_needed(timeout=3000)
                await handle.click(timeout=kwargs.get("timeout_ms", 10_000))
                return ActResult(ok=True, detail="clicked")

            if action == "type":
                await handle.scroll_into_view_if_needed(timeout=3000)
                await handle.fill("")
                await handle.fill(str(kwargs["value"]))
                return ActResult(ok=True, detail="typed")

            if action == "select":
                await handle.select_option(str(kwargs["value"]))
                return ActResult(ok=True, detail="selected")

            if action == "extract":
                kind = kwargs.get("kind", "text")
                if kind == "value":
                    out = await handle.evaluate("el => el.value ?? ''")
                elif kind == "attribute":
                    out = await handle.get_attribute(kwargs["attribute"]) or ""
                else:
                    out = await handle.inner_text()
                return ActResult(ok=True, detail="extracted", extracted=str(out).strip())

            return ActResult(ok=False, detail=f"unknown action {action!r}")

        except PWError as exc:
            return ActResult(ok=False, detail=f"{type(exc).__name__}: {exc}".split("\n")[0])

    # -- conditions --------------------------------------------------------

    async def _all_text(self) -> str:
        chunks: list[str] = []
        for frame in self.page.frames:
            try:
                chunks.append(await frame.evaluate("() => document.body ? document.body.innerText : ''"))
            except PWError:
                continue
        return "\n".join(chunks)

    async def check(self, condition: Condition) -> bool:
        """Evaluate one condition against the live surface."""
        if condition.kind in ("text_present", "text_absent"):
            text = await self._all_text()
            needle = condition.text or ""
            if not condition.case_sensitive:
                text, needle = text.lower(), needle.lower()
            found = needle in text
            return found if condition.kind == "text_present" else not found

        if condition.kind in ("element_present", "element_absent"):
            desc = ElementDescriptor(
                primary=LocatorSignal(
                    by="role_name", role=condition.role, name=condition.name
                )
            )
            res = await self.resolve(desc)
            found = isinstance(res, (Resolved, Ambiguous))
            return found if condition.kind == "element_present" else not found

        if condition.kind == "url_matches":
            return bool(re.search(condition.pattern or "", self.page.url))

        if condition.kind == "title_matches":
            return bool(re.search(condition.pattern or "", await self.page.title()))

        return False  # pragma: no cover

    async def wait_for(self, condition: Condition, timeout_ms: int) -> bool:
        """Poll a condition. Never a fixed sleep."""
        deadline = timeout_ms
        step = 200
        while deadline > 0:
            if await self.check(condition):
                return True
            await self.page.wait_for_timeout(step)
            deadline -= step
        return await self.check(condition)

    # -- evidence ----------------------------------------------------------

    async def snapshot_evidence(self, label: str) -> dict[str, str]:
        if self.evidence_dir is None:
            return {}
        self._shot_seq += 1
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", label)[:48]
        shots = self.evidence_dir / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"{self._shot_seq:03d}_{safe}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=False)
        except PWError:
            return {}
        return {"screenshot": str(path.relative_to(self.evidence_dir))}

    async def capture_descriptor(self, ref_element: ObservedElement) -> CapturedEvidence:
        """Record what an element looked like at discovery time."""
        return CapturedEvidence(
            role=ref_element.role,
            name=ref_element.name,
            tag=ref_element.tag,
            bbox=ref_element.bbox,
            frame_url=next(
                (f.url for f in self.page.frames if self._frame_path(f) == ref_element.frame_path),
                None,
            ),
        )

    async def raw_signals(self, element: ObservedElement) -> dict:
        """Every identifying signal available for one observed element.

        Returns raw facts only. Deciding which of them makes the best primary
        locator is a *policy* question and lives in cua.discovery.descriptors,
        so that the ranking can be tuned without touching the driver.
        """
        for frame in self.page.frames:
            if self._frame_path(frame) != element.frame_path:
                continue
            try:
                got = await frame.evaluate(DESCRIBE_JS, {"ref": element.ref % 10_000})
            except PWError:
                continue
            if got:
                return got
        return {}

    async def dom_snapshot(self, path: Path) -> str | None:
        """Full HTML of every frame. The richer signal captured on failure."""
        try:
            parts = []
            for frame in self.page.frames:
                parts.append(f"<!-- FRAME {frame.name or '(main)'} {frame.url} -->")
                parts.append(await frame.content())
            path.write_text("\n\n".join(parts), encoding="utf-8")
            return str(path.name)
        except (PWError, OSError):
            return None
