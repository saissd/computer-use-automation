"""Locator resolution: the rules that make replay trustworthy.

Three properties are tested here because they are the ones that, if wrong,
cause automation to do something silently incorrect rather than visibly
broken:

  * the primary signal is preferred when it works;
  * a fallback is used when the primary fails, and the tier is *reported* so
    drift is visible;
  * an ambiguous descriptor is refused, never guessed.
"""

import pytest

from cua.core.models import (
    Disambiguation,
    ElementDescriptor,
    LocatorSignal,
    Scope,
)
from cua.surface.base import Ambiguous, NotFound, Resolved

pytestmark = pytest.mark.usefixtures("reset_app")

CONTENT = Scope(frame_path=["content"])


@pytest.fixture
async def on_member_page(session, goto):
    """Navigate to a member detail page and hand back the surface."""
    frame = await goto(session)
    await frame.fill("#ctl00_ContentPlaceHolder1_txtMbrNo", "12345")
    await frame.click("input[value='Search']")
    await session.page.wait_for_timeout(500)
    return session.surface


# --------------------------------------------------------------------------


async def test_primary_role_name_wins(session, goto):
    await goto(session)
    desc = ElementDescriptor(
        primary=LocatorSignal(by="role_name", role="textbox", name="Member Number", exact=True),
        fallbacks=[LocatorSignal(by="css", selector="#ctl00_ContentPlaceHolder1_txtMbrNo")],
        scope=CONTENT,
    )

    res = await session.surface.resolve(desc)

    assert isinstance(res, Resolved)
    assert res.tier == 0, "primary should have resolved it; a fallback means drift"


async def test_fallback_is_used_and_its_tier_is_reported(session, goto):
    """When the primary signal stops working, the run survives and says so."""
    await goto(session)
    desc = ElementDescriptor(
        # Simulates the field losing its accessible name in a vendor upgrade.
        primary=LocatorSignal(by="role_name", role="textbox", name="Member No.", exact=True),
        fallbacks=[
            LocatorSignal(by="text_near", anchor="Member Number", direction="right", offset=1),
            LocatorSignal(by="css", selector="#ctl00_ContentPlaceHolder1_txtMbrNo"),
        ],
        scope=CONTENT,
    )

    res = await session.surface.resolve(desc)

    assert isinstance(res, Resolved)
    assert res.tier == 1, "should have fallen through to text_near"
    # The tier is what the drift report is built from.
    assert "Member Number" in res.signal


async def test_ambiguous_descriptor_is_refused_not_guessed(on_member_page):
    surface = on_member_page
    desc = ElementDescriptor(
        primary=LocatorSignal(by="css", selector="input.btn"),  # two buttons match
        scope=CONTENT,
    )

    res = await surface.resolve(desc)

    assert isinstance(res, Ambiguous)
    assert res.count == 2


async def test_disambiguation_resolves_a_legitimate_multi_match(on_member_page):
    surface = on_member_page
    desc = ElementDescriptor(
        primary=LocatorSignal(by="css", selector="input.btn"),
        disambiguation=Disambiguation(nth=0),
        scope=CONTENT,
    )

    res = await surface.resolve(desc)

    assert isinstance(res, Resolved)


async def test_unknown_element_reports_every_tier_tried(session, goto):
    await goto(session)
    desc = ElementDescriptor(
        primary=LocatorSignal(by="role_name", role="textbox", name="Nope"),
        fallbacks=[LocatorSignal(by="css", selector="#also-nope")],
    )

    res = await session.surface.resolve(desc)

    assert isinstance(res, NotFound)
    assert len(res.tried) == 2


async def test_text_near_reaches_into_a_grid_row(on_member_page):
    """The Regular Savings balance has no name, no id, and no label."""
    surface = on_member_page
    desc = ElementDescriptor(
        primary=LocatorSignal(
            by="text_near", anchor="Regular Savings", direction="right", offset=2
        ),
        scope=CONTENT,
    )

    res = await surface.resolve(desc)
    assert isinstance(res, Resolved)

    act = await surface.act("extract", res, kind="text")
    assert act.extracted == "$8412.55"


async def test_frame_scope_is_honoured(session, goto):
    """The same role+name in the wrong frame must not satisfy a scoped descriptor."""
    await goto(session)
    obs = await session.surface.observe()

    # The nav frame and the content frame are genuinely separate documents.
    frames = {tuple(f.path) for f in obs.frames}
    assert ("nav",) in frames and ("content",) in frames

    scoped = ElementDescriptor(
        primary=LocatorSignal(by="role_name", role="link", name="Member Search"),
        scope=Scope(frame_path=["content"]),
    )
    res = await session.surface.resolve(scoped)
    # "Member Search" is a link in the *nav* frame only.
    assert isinstance(res, NotFound)


async def test_observation_hash_is_stable_and_change_sensitive(session, goto):
    frame = await goto(session)
    a = await session.surface.observe()
    b = await session.surface.observe()
    assert a.state_hash == b.state_hash, "same state must hash the same (stuck detection)"

    await frame.fill("#ctl00_ContentPlaceHolder1_txtMbrNo", "12345")
    c = await session.surface.observe()
    assert c.state_hash != a.state_hash, "a changed field must change the hash"


async def test_password_values_are_never_observed(session):
    """Redaction starts at perception, not at logging."""
    await session.surface.act("navigate", None, url="http://127.0.0.1:8099/logout")
    await session.page.fill("#ctl00_txtPasswd", "hunter2")

    obs = await session.surface.observe()

    assert "hunter2" not in obs.text
    assert all("hunter2" != (e.value or "") for e in obs.elements)
