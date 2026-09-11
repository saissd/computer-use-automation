"""Guardrails: the allowlist, risk classification, and redaction.

These run without a browser. They are the cheapest tests in the suite and they
cover the controls whose failure mode is a compliance incident rather than a
broken run, which is a good reason to make them cheap enough to run constantly.
"""

import pytest

from cua.core.models import Capability, InputParam
from cua.policy.engine import Decision, PolicyConfig, PolicyEngine
from cua.policy.redaction import REDACTED, Redactor
from cua.replay.engine import InputValidationError, substitute, validate_inputs

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def engine():
    return PolicyEngine(
        PolicyConfig(
            allowed_domains=["127.0.0.1:8099"],
            allowed_routes=["/", "/search", "/member/*"],
        )
    )


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("http://127.0.0.1:8099/search", True),
        ("http://127.0.0.1:8099/member/12345", True),
        ("http://127.0.0.1:8099/_control/inject", False),   # test endpoint, off limits
        ("http://127.0.0.1:9999/search", False),            # wrong port
        ("http://evil.example.com/search", False),          # wrong host
        ("https://127.0.0.1:8099/member/1/../../etc", False),
    ],
)
def test_url_allowlist(engine, url, allowed):
    assert engine.check_url(url).allowed is allowed


def test_denied_action_types_are_refused(engine):
    assert not engine.check_action_type("download").allowed
    assert not engine.check_action_type("execute_script").allowed
    assert engine.check_action_type("click").allowed


def test_unknown_action_type_is_denied_by_default(engine):
    """Default-deny: a verb nobody listed is not permitted."""
    assert not engine.check_action_type("drag_and_drop").allowed


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Search", "read_only"),
        ("New Search", "read_only"),
        ("Create Sub-Account", "reversible_write"),
        ("Open Sub-Account", "reversible_write"),
        ("Transfer Funds", "irreversible"),
        ("Delete Member", "irreversible"),
        ("Close Account", "irreversible"),
        ("Authorize Wire", "irreversible"),
    ],
)
def test_risk_classification_reads_the_control_label(engine, label, expected):
    """The signal is the text a human operator would read before clicking."""
    assert engine.classify("click", label) == expected


def test_irreversible_requires_approval(engine):
    decision = engine.check("click", label="Transfer Funds")
    assert decision.verdict == "require_approval"
    assert decision.risk == "irreversible"


def test_irreversible_proceeds_once_approved(engine):
    assert engine.check("click", label="Transfer Funds", approved=True).allowed


def test_reversible_write_proceeds_but_is_labelled(engine):
    decision = engine.check("click", label="Create Sub-Account")
    assert decision.allowed
    assert decision.risk == "reversible_write"


def test_typing_is_a_reversible_write_not_a_commit(engine):
    """Filling a field mutates nothing durable; the submit click is the commit."""
    assert engine.classify("type") == "reversible_write"
    assert engine.classify("extract") == "read_only"


def test_unknown_policy_key_fails_loudly(tmp_path):
    """A security control that silently ignores a typo is worse than none."""
    path = tmp_path / "policy.yaml"
    path.write_text("allowed_domans: [x]\n", encoding="utf-8")  # typo
    with pytest.raises(ValueError, match="unknown policy keys"):
        PolicyConfig.load(path)


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_declared_sensitive_values_are_scrubbed():
    r = Redactor({"12345"})
    assert "12345" not in r.text("looked up member 12345 ok")
    assert REDACTED in r.text("looked up member 12345 ok")


def test_patterns_catch_undeclared_regulated_data():
    r = Redactor()
    assert "123-45-6789" not in r.text("SSN 123-45-6789")
    assert "4111111111111111" not in r.text("card 4111111111111111")
    assert "0001234501" not in r.text("account 0001234501")
    assert "a@b.example.com" not in r.text("email a@b.example.com")


def test_redaction_recurses_through_structures():
    r = Redactor({"secret-token"})
    out = r.obj({"a": ["secret-token", {"b": "SSN 123-45-6789"}]})
    assert "secret-token" not in str(out)
    assert "123-45-6789" not in str(out)


def test_pii_logging_keeps_shape_but_not_content():
    """Length is useful for debugging and discloses nothing."""
    assert Redactor.for_logging("member_id", "12345", "pii") == "[REDACTED:member_id:len=5]"
    assert Redactor.for_logging("password", "hunter2", "secret") == REDACTED
    assert Redactor.for_logging("branch", "Riverbend", "internal") == "Riverbend"


def test_short_values_are_not_used_as_redaction_keys():
    """Redacting a 1-2 char value would destroy every log line it appears in."""
    r = Redactor({"7"})
    assert r.text("balance was 7 dollars") == "balance was 7 dollars"


# --------------------------------------------------------------------------
# Input contract
# --------------------------------------------------------------------------


def _cap(**kw) -> Capability:
    from cua.core.models import Condition, Step, Target

    return Capability(
        id="t", name="t", description="t",
        target=Target(app_id="t", entry="http://127.0.0.1:8099/"),
        steps=[Step(id="s1", intent="go", action="navigate", url="http://127.0.0.1:8099/")],
        checkpoint=Condition(kind="text_present", text="x"),
        **kw,
    )


def test_pattern_is_enforced():
    cap = _cap(inputs=[InputParam(name="member_id", pattern=r"\d{4,9}")])
    assert validate_inputs(cap, {"member_id": "12345"}) == {"member_id": "12345"}
    with pytest.raises(InputValidationError, match="pattern"):
        validate_inputs(cap, {"member_id": "abc"})


def test_missing_required_input_is_rejected():
    cap = _cap(inputs=[InputParam(name="member_id")])
    with pytest.raises(InputValidationError, match="missing required"):
        validate_inputs(cap, {})


def test_unknown_input_is_rejected():
    cap = _cap(inputs=[InputParam(name="member_id")])
    with pytest.raises(InputValidationError, match="unknown"):
        validate_inputs(cap, {"member_id": "1234", "extra": "x"})


def test_numeric_types_are_checked():
    cap = _cap(inputs=[InputParam(name="amount", type="money")])
    assert validate_inputs(cap, {"amount": "1,250.00"})
    with pytest.raises(InputValidationError, match="numeric"):
        validate_inputs(cap, {"amount": "lots"})


def test_substitution_fills_placeholders():
    assert substitute("{{member_id}}", {"member_id": "12345"}) == "12345"
    assert substitute("/member/{{member_id}}/x", {"member_id": "7"}) == "/member/7/x"
    assert substitute(None, {}) is None


def test_substitution_refuses_undeclared_parameters():
    """A step referring to a parameter the contract does not declare is a bug."""
    with pytest.raises(InputValidationError, match="undeclared input"):
        substitute("{{nope}}", {"member_id": "1"})
