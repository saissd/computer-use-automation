"""The discovery loop, driven by a scripted stand-in for the model.

No API key, no network, no cost — and it still exercises the part of discovery
that can actually be wrong. The model's *judgement* is not what breaks; the
machinery around it is: resolving a ref to a real element, building a
descriptor that is unambiguous, accreting a typed step, inferring a
post-assertion that is not contaminated with this run's data, and applying the
generalisation pass without corrupting the recorded steps.

The stub reads the observation the way the real model does — it parses the
numbered control list and picks a control by its accessible name — so it
cannot cheat by knowing refs in advance. If the observation renderer breaks,
this test breaks.

`test_discovered_artifact_replays` is the important one: it closes the loop by
replaying what discovery produced. That is the same self-check `cu discover`
runs before saving.
"""

import re
import types

import pytest

from cua.discovery.agent import DiscoveryAgent
from cua.discovery.generalize import apply_generalization
from cua.discovery.profiles import load_recovery_profile
from cua.evidence.writer import EvidenceWriter
from cua.replay.engine import ReplayEngine, new_run_id
from tests.conftest import APP_PASSWORD, APP_USER, BASE_URL, ROOT

REF_LINE = re.compile(r"\[(\d+)\]\s+(\S+)\s+\"([^\"]*)\"")


def _find_ref(observation_text: str, role: str, name: str) -> int:
    """Pick a control out of the rendered observation, as the model would."""
    for ref, got_role, got_name in REF_LINE.findall(observation_text):
        if got_role == role and got_name == name:
            return int(ref)
    raise AssertionError(f"no {role} named {name!r} in observation:\n{observation_text}")


CELL_LINE = re.compile(r"\[(\d+)\] cell value='([^']*)' row='([^']*)'")


def _find_cell(observation_text: str, row_contains: str, value_prefix: str) -> int:
    """Pick a readable value by the row it sits in, as the model would."""
    for ref, value, row in CELL_LINE.findall(observation_text):
        if row_contains in row and value.startswith(value_prefix):
            return int(ref)
    raise AssertionError(f"no cell in a {row_contains!r} row:\n{observation_text}")


class ScriptedModel:
    """Minimal stand-in for `anthropic.Anthropic().messages`.

    Returns one tool call per turn from a fixed plan, resolving refs against
    the observation it was just given.
    """

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        last = kwargs["messages"][-1]["content"]
        text = last if isinstance(last, str) else last[0]["content"]

        name, build = self.plan[len(self.calls)]
        args = build(text)
        self.calls.append((name, args))

        block = types.SimpleNamespace(
            type="tool_use", name=name, input=args, id=f"toolu_{len(self.calls)}"
        )
        return types.SimpleNamespace(content=[block], stop_reason="tool_use")


PLAN = [
    (
        "type",
        lambda text: {
            "ref": _find_ref(text, "textbox", "Member Number"),
            "text": "12345",
            "intent": "Type the member number into the search field",
        },
    ),
    (
        "click",
        lambda text: {
            "ref": _find_ref(text, "button", "Search"),
            "intent": "Run the member search",
        },
    ),
    (
        "extract",
        lambda text: {
            "ref": _find_cell(text, "Regular Savings", "$"),
            "output_name": "savings_balance",
            "intent": "Read the Regular Savings balance",
        },
    ),
    (
        "done",
        lambda text: {
            "summary": "Opened the member relationship summary.",
            "success_text": "Member Relationship Summary",
        },
    ),
]


@pytest.fixture
def agent(session, policy, tmp_path):
    def _build(plan):
        ev = EvidenceWriter(new_run_id("disco"), tmp_path)
        handlers = load_recovery_profile(
            "meridian-core-membersvc", ROOT / "config" / "recovery_profiles.yaml"
        )
        return (
            DiscoveryAgent(
                session.surface, policy, session.lease, ev,
                client=ScriptedModel(plan), model="scripted",
                recovery_handlers=handlers,
            ),
            ev,
        )

    return _build


# --------------------------------------------------------------------------


async def test_discovery_accretes_a_replayable_artifact(agent):
    """Steps are captured from real elements, not written by the model."""
    a, _ = agent(PLAN[:2] + [PLAN[3]])
    result = await a.run(
        "look up member 12345", BASE_URL + "/", "meridian-core-membersvc"
    )

    assert result.status == "success", result.reason
    cap = result.capability
    assert cap is not None

    # s0 is the mechanically-prepended navigate; then type, then click.
    assert [s.action for s in cap.steps] == ["navigate", "type", "click"]
    assert cap.steps[1].value == "12345"
    assert cap.steps[1].intent.startswith("Type the member number")

    # The descriptor was captured from the live element, and the model never
    # saw or supplied a locator.
    target = cap.steps[1].target
    assert target.primary.by == "role_name"
    assert target.primary.name == "Member Number"
    assert target.scope.frame_path == ["content"]
    # Ranked fallbacks, ending in the generated id as a last resort.
    assert any(s.by == "css" and "ctl00" in (s.selector or "") for s in target.fallbacks)
    assert target.captured.tag == "input"


async def test_every_step_inherits_the_app_recovery_profile(agent):
    a, _ = agent(PLAN[:2] + [PLAN[3]])
    result = await a.run("look up member 12345", BASE_URL + "/", "meridian-core-membersvc")

    for step in result.capability.steps:
        kinds = {r.do for r in step.on_error}
        assert "reauth" in kinds, f"step {step.id} did not inherit session handling"
        assert "fail" in kinds, f"step {step.id} did not inherit app-error handling"


async def test_inferred_post_assert_is_not_contaminated_with_run_data(agent):
    """A checkpoint containing this run's member id would break every other input."""
    a, _ = agent(PLAN[:2] + [PLAN[3]])
    result = await a.run("look up member 12345", BASE_URL + "/", "meridian-core-membersvc")

    click = next(s for s in result.capability.steps if s.action == "click")
    assert click.post_assert is not None, "a navigating click must be verified"
    text = click.post_assert.text or ""

    assert "12345" not in text, "the typed value must not appear"
    assert "Dana" not in text, "the member name is per-run data"
    # The subtler failure: an assertion that mentions no *typed* value but is
    # still record-specific. An earlier version of the inferrer chose
    # "Branch\tCedar Falls Main\t\tMember Since\t2011-03-14", which passes for
    # member 12345 and fails for everyone else.
    assert "\t" not in text, "a tab means a table row, which means record data"
    assert not any(c.isdigit() for c in text), "digits on a detail screen are data"
    assert "Cedar Falls" not in text


async def test_checkpoint_rejects_a_data_bearing_success_text(agent):
    """If the model proposes a checkpoint containing typed data, we override it."""
    plan = PLAN[:2] + [
        (
            "done",
            lambda text: {
                "summary": "done",
                "success_text": "Member 12345 summary",  # contaminated
            },
        )
    ]
    a, _ = agent(plan)
    result = await a.run("look up member 12345", BASE_URL + "/", "meridian-core-membersvc")

    assert "12345" not in result.capability.checkpoint.text


async def test_ambiguous_choice_is_refused_and_fed_back_to_the_model(agent, session):
    """A control that cannot be described uniquely is rejected at record time.

    Catching this during discovery is the point: an ambiguous descriptor that
    reaches an artifact is a landmine that goes off in production instead.
    """
    plan = [
        (
            "type",
            lambda text: {
                "ref": _find_ref(text, "textbox", "Member Number"),
                "text": "12345",
                "intent": "type the member number",
            },
        ),
        (
            "click",
            lambda text: {
                "ref": _find_ref(text, "button", "Search"),
                "intent": "search",
            },
        ),
        # Both remaining buttons are unnamed-but-identical in the eyes of a
        # bare css selector; the harness must not record either.
        (
            "escalate",
            lambda text: {"reason": "could not identify a unique control"},
        ),
    ]
    a, _ = agent(plan)
    result = await a.run("x", BASE_URL + "/", "meridian-core-membersvc")

    assert result.status == "escalated"
    assert result.intervention_id
    assert "unique" in result.reason


async def test_policy_blocks_reach_the_model_as_feedback(agent, session):
    """A denied action is reported back, not silently executed."""
    plan = [
        (
            "type",
            lambda text: {
                "ref": _find_ref(text, "textbox", "Member Number"),
                "text": "12345",
                "intent": "type",
            },
        ),
        ("escalate", lambda text: {"reason": "stopping for the test"}),
    ]
    a, ev = agent(plan)
    await a.run("x", BASE_URL + "/", "meridian-core-membersvc")

    events = {e["event"] for e in ev.read_events()}
    assert "discovery_start" in events
    assert "model_decision" in events
    assert "discovery_escalated" in events


async def test_discovery_log_holds_no_run_data(agent):
    """Before generalisation nothing is classified, so the log must assume the worst."""
    a, ev = agent(PLAN)
    result = await a.run("look up member 12345", BASE_URL + "/", "meridian-core-membersvc")
    assert result.status == "success", result.reason

    log = ev.log_path.read_text(encoding="utf-8")
    assert "12345" not in log, "typed member number reached the discovery log"
    assert "8412.55" not in log, "extracted balance reached the discovery log"


async def test_saved_artifact_holds_no_run_data(agent, tmp_path):
    """The model's own words and the recorded HTML both carry this run's data.

    Found on the first live run: the model wrote "look up member 12345" into
    an intent, and the snippet captured from the balance cell was the balance.
    """
    from cua.catalog.catalog import Catalog

    chatty_type = (
        "type",
        lambda text: {
            "ref": _find_ref(text, "textbox", "Member Number"),
            "text": "12345",
            "intent": "Type member 12345 into the search field",
        },
    )
    a, _ = agent([chatty_type] + PLAN[1:])
    result = await a.run("look up member 12345", BASE_URL + "/", "meridian-core-membersvc")
    assert result.status == "success", result.reason

    spec = {
        "id": "lookup", "name": "Lookup", "description": "Look up a member.",
        "risk_class": "read_only", "risk_rationale": "read only",
        "parameters": [{
            "name": "member_id", "literal_value": "12345", "type": "string",
            "description": "", "pattern": "", "sensitivity": "pii",
        }],
        "outputs": [], "outcomes": [],
    }
    final = apply_generalization(result.capability, spec, result.raw_outputs)
    saved = Catalog(tmp_path).save(final).read_text(encoding="utf-8")

    assert "12345" not in saved
    assert "8412.55" not in saved
    assert "{{member_id}}" in final.steps[1].intent


async def test_generalization_parameterises_without_touching_steps(agent):
    """The model names and parameterises; it does not rewrite the flow."""
    a, _ = agent(PLAN[:2] + [PLAN[3]])
    result = await a.run("look up member 12345", BASE_URL + "/", "meridian-core-membersvc")
    draft = result.capability
    before = [(s.id, s.action, s.target.describe() if s.target else None) for s in draft.steps]

    spec = {
        "id": "lookup_member",
        "name": "Look up a member",
        "description": "Open a member's relationship summary.",
        "risk_class": "read_only",
        "risk_rationale": "Read-only lookup.",
        "parameters": [
            {
                "name": "member_id",
                "literal_value": "12345",
                "type": "string",
                "description": "Institution member number.",
                "pattern": r"\d{4,9}",
                "sensitivity": "pii",
            },
            # A parameter the run never typed: must be dropped, because an
            # input that reaches no step is a lie in the contract.
            {
                "name": "phantom",
                "literal_value": "never-typed",
                "type": "string",
                "description": "",
                "pattern": "",
                "sensitivity": "internal",
            },
        ],
        "outputs": [],
        "outcomes": [
            {
                "name": "member_not_found",
                "description": "No such member.",
                "detect_text": "No member record found",
            }
        ],
    }

    final = apply_generalization(draft, spec, {})

    assert final.id == "lookup_member"
    assert [p.name for p in final.inputs] == ["member_id"], "phantom must be dropped"
    assert final.inputs[0].sensitivity == "pii"
    assert final.steps[1].value == "{{member_id}}", "literal should be templated"
    assert [o.name for o in final.outcomes] == ["member_not_found"]
    assert final.status == "draft", "a fresh recording is never auto-approved"

    after = [(s.id, s.action, s.target.describe() if s.target else None) for s in final.steps]
    assert before == after, "generalisation must not alter the recorded steps"


async def test_discovered_artifact_replays(agent, session, policy, tmp_path):
    """Close the loop: what discovery recorded actually works on replay.

    This is the self-check `cu discover --verify` performs before saving.
    """
    a, _ = agent(PLAN)
    result = await a.run("look up member 12345", BASE_URL + "/", "meridian-core-membersvc")
    assert result.status == "success", result.reason

    # The value is anchored on the row's label, not its neighbour: the cell
    # immediately left of a balance is the account status, which is data.
    extract = next(s for s in result.capability.steps if s.action == "extract")
    assert extract.target.primary.by == "text_near"
    assert (extract.target.primary.anchor, extract.target.primary.offset) == ("Regular Savings", 2)

    spec = {
        "id": "discovered_lookup",
        "name": "Discovered lookup",
        "description": "Open a member's relationship summary.",
        "risk_class": "read_only",
        "risk_rationale": "read only",
        "parameters": [
            {
                "name": "member_id",
                "literal_value": "12345",
                "type": "string",
                "description": "Member number.",
                "pattern": r"\d{4,9}",
                "sensitivity": "pii",
            }
        ],
        "outputs": [],
        "outcomes": [
            {
                "name": "member_not_found",
                "description": "No such member.",
                "detect_text": "No member record found",
            }
        ],
    }
    capability = apply_generalization(result.capability, spec, {})

    ev = EvidenceWriter(new_run_id("verify"), tmp_path)
    engine = ReplayEngine(
        session.surface, policy, session.lease, ev,
        reauth=session.reauth_hook(BASE_URL, APP_USER, APP_PASSWORD),
    )

    # The recorded flow, replayed with a *different* member than it was
    # recorded with — which is the whole point of parameterisation.
    replayed = await engine.run(capability, {"member_id": "23456"})

    assert replayed.status == "success", replayed.summary()
    assert replayed.outputs["savings_balance"].startswith("$")
    assert all((s.locator_tier or 0) == 0 for s in replayed.steps if s.locator_tier is not None)

    # And the declared business outcome works on the artifact discovery produced.
    missing = await ReplayEngine(
        session.surface, policy, session.lease,
        EvidenceWriter(new_run_id("verify2"), tmp_path),
    ).run(capability, {"member_id": "99999"})
    assert missing.status == "business_outcome"
    assert missing.outcome == "member_not_found"
