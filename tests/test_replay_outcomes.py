"""The error taxonomy, one test per class.

This file is the evidence for the central claim of the design: that replay
distinguishes *expected business outcomes* from *recoverable conditions* from
*hard failures*, and responds to each deliberately.

Read the assertions as a specification:

    success            happy path, typed outputs returned
    business_outcome   member_not_found, permission_denied
    recovered          maintenance interstitial, transient slowness
    session            expired -> re-authenticated, then completes
    failed             app 500, ambiguous descriptor, bad input
    needs_human        policy block, irreversible without approval
"""

import pytest

from cua.core.results import ErrorClass

MEMBER_OK = "12345"
MEMBER_MISSING = "99999"
MEMBER_RESTRICTED = "55555"


# --------------------------------------------------------------------------
# Success
# --------------------------------------------------------------------------


async def test_happy_path_returns_typed_outputs(replay, capability):
    result = await replay(capability, {"member_id": MEMBER_OK})

    assert result.status == "success", result.summary()
    assert result.outputs["member_name"] == "Dana Whitfield"
    assert result.outputs["savings_balance"] == "$8412.55"
    assert [s.status for s in result.steps] == ["ok"] * 5
    # Every locator resolved on its primary signal: no drift.
    assert all((s.locator_tier or 0) == 0 for s in result.steps if s.locator_tier is not None)


async def test_second_member_proves_parameterisation(replay, capability):
    result = await replay(capability, {"member_id": "23456"})

    assert result.status == "success"
    assert result.outputs["member_name"] == "Marcus Oyelaran"
    assert result.outputs["savings_balance"] == "$214.09"


async def test_replay_is_deterministic_across_runs(replay, capability):
    """Same inputs, same steps, same outputs — twice."""
    first = await replay(capability, {"member_id": MEMBER_OK})
    second = await replay(capability, {"member_id": MEMBER_OK})

    assert first.outputs == second.outputs
    assert [(s.step_id, s.status, s.locator_tier) for s in first.steps] == [
        (s.step_id, s.status, s.locator_tier) for s in second.steps
    ]


# --------------------------------------------------------------------------
# Business outcomes — legitimate answers, NOT failures
# --------------------------------------------------------------------------


async def test_missing_member_is_a_business_outcome_not_a_crash(replay, capability):
    result = await replay(capability, {"member_id": MEMBER_MISSING})

    assert result.status == "business_outcome", result.summary()
    assert result.outcome == "member_not_found"
    assert "does not exist" in (result.outcome_detail or "")
    # The distinction the whole design turns on:
    assert result.ok is True
    assert result.error is None


async def test_permission_denied_is_distinct_from_not_found(replay, capability):
    result = await replay(capability, {"member_id": MEMBER_RESTRICTED})

    assert result.status == "business_outcome"
    assert result.outcome == "permission_denied"
    assert result.outcome != "member_not_found"


# --------------------------------------------------------------------------
# Recoverable conditions — detected, handled, run still completes
# --------------------------------------------------------------------------


async def test_maintenance_interstitial_is_dismissed_and_run_succeeds(
    replay, capability, arm
):
    arm("dialog")
    result = await replay(capability, {"member_id": MEMBER_OK})

    assert result.status == "success", result.summary()
    recovered = [s for s in result.steps if s.recoveries_applied]
    assert recovered, "expected the interstitial handler to fire"
    assert any("dismiss" in r for s in recovered for r in s.recoveries_applied)


async def test_transient_slowness_is_waited_out(replay, capability, arm):
    arm("slow")
    result = await replay(capability, {"member_id": MEMBER_OK})

    # An 8s stall must not fail the run: waits are on conditions, not sleeps.
    assert result.status == "success", result.summary()
    assert result.outputs["savings_balance"] == "$8412.55"


async def test_session_expiry_triggers_reauth_then_completes(replay, capability, arm):
    arm("timeout")
    result = await replay(capability, {"member_id": MEMBER_OK})

    assert result.status == "success", result.summary()
    reauthed = [
        r for s in result.steps for r in s.recoveries_applied if "reauth" in r
    ]
    assert reauthed, "expected the session-expiry handler to re-authenticate"


# --------------------------------------------------------------------------
# Hard failures — stop, and say exactly what broke
# --------------------------------------------------------------------------


async def test_application_error_is_a_hard_failure_with_debug_detail(
    replay, capability, arm
):
    arm("error500")
    result = await replay(capability, {"member_id": MEMBER_OK})

    assert result.status == "failed", result.summary()
    assert result.ok is False
    err = result.error
    assert err is not None
    assert err.step_id, "a failure must say which step"
    assert err.expected and err.observed, "a failure must be debuggable"


async def test_invalid_input_fails_before_touching_the_browser(replay, capability):
    result = await replay(capability, {"member_id": "not-a-number"})

    assert result.status == "failed"
    assert result.error.klass == ErrorClass.INVALID_INPUT
    assert result.steps == []  # nothing was attempted


async def test_unknown_input_is_rejected(replay, capability):
    result = await replay(capability, {"member_id": MEMBER_OK, "nope": "x"})

    assert result.status == "failed"
    assert result.error.klass == ErrorClass.INVALID_INPUT
    assert "nope" in result.error.message


async def test_ambiguous_descriptor_refuses_to_guess(replay, capability, session):
    """A descriptor matching several controls is a defect, not a coin flip."""
    from cua.core.models import ElementDescriptor, LocatorSignal

    broken = capability.model_copy(deep=True)
    # The member detail page carries two buttons, "Open Sub-Account" and
    # "New Search". A selector with no distinguishing signal matches both, and
    # acting on whichever came first is exactly the bug this rule prevents.
    broken.steps[3].target = ElementDescriptor(
        primary=LocatorSignal(by="css", selector="input.btn")
    )

    result = await replay(broken, {"member_id": MEMBER_OK})

    assert result.status == "failed", result.summary()
    assert result.error.klass == ErrorClass.ELEMENT_AMBIGUOUS
    assert "refusing to guess" in result.error.message


async def test_missing_element_reports_every_tier_it_tried(replay, capability):
    from cua.core.models import ElementDescriptor, LocatorSignal

    broken = capability.model_copy(deep=True)
    broken.steps[1].target = ElementDescriptor(
        primary=LocatorSignal(by="role_name", role="textbox", name="Nonexistent Field"),
        fallbacks=[LocatorSignal(by="css", selector="#no-such-id")],
    )

    result = await replay(broken, {"member_id": MEMBER_OK})

    assert result.status == "failed"
    assert result.error.klass == ErrorClass.ELEMENT_NOT_FOUND
    assert "tier0" in result.error.observed and "tier1" in result.error.observed


async def test_checkpoint_failure_is_reported_as_such(replay, capability):
    from cua.core.models import Condition

    broken = capability.model_copy(deep=True)
    broken.checkpoint = Condition(kind="text_present", text="This text never appears")

    result = await replay(broken, {"member_id": MEMBER_OK})

    assert result.status == "failed"
    assert result.error.klass == ErrorClass.CHECKPOINT_FAILED
    assert "This text never appears" in result.error.expected


# --------------------------------------------------------------------------
# Policy — blocked work routes to a human rather than failing silently
# --------------------------------------------------------------------------


async def test_out_of_allowlist_navigation_needs_a_human(replay, capability):
    blocked = capability.model_copy(deep=True)
    blocked.steps[0].url = "http://127.0.0.1:8099/_control/inject"
    blocked.steps[0].post_assert = None

    result = await replay(blocked, {"member_id": MEMBER_OK})

    assert result.status == "needs_human", result.summary()
    assert result.intervention_id
    assert "allowlist" in result.reason.lower() or "policy" in result.reason.lower()


async def test_irreversible_capability_is_not_run_unattended(replay, capability):
    risky = capability.model_copy(deep=True)
    risky.risk.klass = "irreversible"

    result = await replay(risky, {"member_id": MEMBER_OK})

    assert result.status == "needs_human"
    assert "irreversible" in result.reason


async def test_irreversible_capability_runs_once_approved(replay, capability):
    risky = capability.model_copy(deep=True)
    risky.risk.klass = "irreversible"

    result = await replay(risky, {"member_id": MEMBER_OK}, approved=True)

    assert result.status == "success", result.summary()


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


async def test_failure_captures_richer_evidence(replay, capability, arm):
    arm("error500")
    result = await replay(capability, {"member_id": MEMBER_OK})

    assert result.status == "failed"
    evidence_dir = result.evidence.directory
    from pathlib import Path

    files = {p.name for p in Path(evidence_dir).rglob("*")}
    assert "run.jsonl" in files
    assert "result.json" in files
    assert "dom.html" in files, "a hard failure should capture a DOM snapshot"


async def test_pii_never_reaches_the_run_log(replay, capability):
    """The member id is declared `pii`; it must not appear raw on disk."""
    from pathlib import Path

    result = await replay(capability, {"member_id": MEMBER_OK})
    assert result.status == "success"

    log = Path(result.evidence.directory, "run.jsonl").read_text(encoding="utf-8")
    assert MEMBER_OK not in log, "raw PII leaked into the structured log"
    assert "REDACTED" in log
