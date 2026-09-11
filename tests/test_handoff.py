"""Escalation, human takeover of the live session, and resume.

The scenario is the realistic one: the application session expires mid-replay
and re-authentication is unavailable to the automation (no credentials are
configured for unattended re-auth). No amount of retrying fixes that. A person
has to sign in.

What this test asserts, in order:

  1. the run detects the condition and stops rather than flailing;
  2. an intervention is raised carrying enough context to act on;
  3. taking control genuinely moves the lease, and while a human holds it the
     automation is *blocked* — not asked politely to wait;
  4. the human works in the same live session (same browser context, same
     cookie jar) — which is why signing in there actually unblocks the run;
  5. handing back resumes the run on that session and it completes;
  6. what the human did is recorded independently of what they said they did.

Point 3 is the one worth dwelling on. The lease is not a flag the automation
consults out of courtesy; `assert_automation()` raises, and it is called
before every action.
"""

import asyncio

import pytest

from cua.session.intervention import InterventionQueue
from cua.session.lease import LeaseLost

MEMBER = "12345"


async def _wait_for_intervention(queue: InterventionQueue, timeout_s: float = 20.0):
    for _ in range(int(timeout_s * 10)):
        open_ones = [i for i in queue.list(open_only=True)]
        if open_ones:
            return open_ones[0]
        await asyncio.sleep(0.1)
    raise AssertionError("no intervention was raised")


async def test_human_takes_over_live_session_and_hands_back(
    replay, capability, session, arm
):
    queue = InterventionQueue()
    arm("timeout")

    run = asyncio.create_task(
        replay(
            capability,
            {"member_id": MEMBER},
            escalation_mode="wait",
            queue=queue,
            reauth=False,
            escalation_timeout_s=60,
        )
    )

    # 1 + 2: the run stops and routes an intervention with context.
    req = await _wait_for_intervention(queue)
    assert req.source == "replay"
    assert req.capability == capability.ref
    assert req.step_id, "the operator must be told where it stopped"
    assert req.session_id == session.session_id
    assert req.observation_summary, "the operator must be told what it was looking at"
    # Context is carried, but the member id is PII and must not be in it raw.
    assert MEMBER not in str(req.redacted_inputs)

    # 3: taking control moves the lease, and automation is then blocked.
    assert session.lease.held_by_automation()
    queue.take(req.id, "casey.operator")
    session.lease.grant_to_human("casey.operator", note="signing back in by hand")

    assert not session.lease.held_by_automation()
    with pytest.raises(LeaseLost):
        session.lease.assert_automation()

    # 4: the human works in the *same* live session.
    page = session.page
    await page.goto("http://127.0.0.1:8099/login", wait_until="domcontentloaded")
    await page.fill("#ctl00_txtUserId", "teller01")
    await page.fill("#ctl00_txtPasswd", "training-only")
    await page.click("input[value='Sign On']")
    await page.wait_for_load_state("domcontentloaded")

    # 5: hand back and let the run resume.
    session.lease.return_to_automation(note="signed in, over to you")
    queue.resolve(req.id, "resume")

    result = await asyncio.wait_for(run, timeout=90)

    assert result.status == "success", result.summary()
    assert result.outputs["member_name"] == "Dana Whitfield"

    # 6: the handoff is evidenced.
    resolved = queue.get(req.id)
    assert resolved.state == "resolved"
    assert resolved.resolution == "resume"
    assert resolved.human_actions, "what the human did must be recorded"
    action = resolved.human_actions[0]
    assert action.operator == "casey.operator"
    # Note the URL is *unchanged* across the handoff — the operator signed in
    # and landed back on the same entry point. That is precisely why the
    # evidence diffs the accessibility tree instead of trusting the address
    # bar: the controls that appeared are what actually proves they got in.
    assert action.observed_url_before == action.observed_url_after
    assert "textbox:Member Number" in action.elements_added
    assert action.text_delta_chars != 0

    owners = [(e.from_owner, e.to_owner) for e in session.lease.history]
    assert ("automation", "human") in owners
    assert ("human", "automation") in owners


async def test_operator_abort_returns_needs_human(replay, capability, session, arm):
    """An operator who cannot fix it aborts, and the caller is told so."""
    queue = InterventionQueue()
    arm("timeout")

    run = asyncio.create_task(
        replay(
            capability,
            {"member_id": MEMBER},
            escalation_mode="wait",
            queue=queue,
            reauth=False,
            escalation_timeout_s=60,
        )
    )
    req = await _wait_for_intervention(queue)
    queue.take(req.id, "casey.operator")
    session.lease.grant_to_human("casey.operator")
    session.lease.return_to_automation()
    queue.resolve(req.id, "abort")

    result = await asyncio.wait_for(run, timeout=60)

    assert result.status == "needs_human"
    assert result.intervention_id == req.id
    assert "aborted" in result.reason


async def test_no_operator_response_is_not_treated_as_approval(
    replay, capability, session, arm
):
    """Silence is never consent. A timed-out escalation stays unresolved."""
    queue = InterventionQueue()
    arm("timeout")

    result = await replay(
        capability,
        {"member_id": MEMBER},
        escalation_mode="wait",
        queue=queue,
        reauth=False,
        escalation_timeout_s=2.0,
    )

    assert result.status == "needs_human"
    assert "no operator response" in result.reason
    assert queue.get(result.intervention_id).state != "resolved"


async def test_production_mode_returns_a_ticket_without_blocking(
    replay, capability, arm
):
    """The default: hand the caller an id, never hold a worker on a human."""
    queue = InterventionQueue()
    arm("timeout")

    result = await asyncio.wait_for(
        replay(
            capability,
            {"member_id": MEMBER},
            escalation_mode="return",
            queue=queue,
            reauth=False,
        ),
        timeout=45,
    )

    assert result.status == "needs_human"
    assert result.intervention_id
    assert queue.get(result.intervention_id).state == "open"


async def test_expired_human_lease_reverts_to_automation(session):
    """An operator who walks away must not wedge the session forever."""
    session.lease.grant_to_human("casey.operator", ttl_seconds=0)

    assert session.lease.held_by_automation(), "expired lease should revert"
    assert any("expired" in e.note for e in session.lease.history)
