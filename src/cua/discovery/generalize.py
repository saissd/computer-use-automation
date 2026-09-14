"""The generalisation pass: from "what happened once" to "a reusable capability".

One model call, doing four narrowly-scoped jobs: name the capability,
parameterise the literals that were really inputs, declare the business
outcomes a caller must handle, and classify the risk.

What it explicitly does *not* do is touch the steps. Those were captured
mechanically and are known to work, because the run they came from actually
succeeded. Letting a model rewrite them at this point would trade a verified
sequence for a plausible one.

Everything the model returns is then applied *mechanically* — string
replacement to insert `{{placeholders}}`, list construction for the contract —
and the result is validated against the schema and immediately replayed. A
capability that cannot survive its own verification replay is never saved as
anything better than a draft.
"""

from typing import Any

from cua.core.models import (
    BusinessOutcome,
    Capability,
    Condition,
    InputParam,
    OutputSpec,
    Risk,
)
from cua.discovery.prompts import (
    EMIT_CAPABILITY_TOOL,
    GENERALIZE_SYSTEM,
    generalize_message,
)
from cua.policy.redaction import Redactor

DEFAULT_MODEL = "claude-opus-5"


def _steps_summary(capability: Capability) -> str:
    lines = []
    for step in capability.steps:
        bits = [f"- {step.id}: [{step.action}] {step.intent}"]
        if step.target:
            bits.append(f"    target: {step.target.describe()}")
        if step.value is not None:
            bits.append(f"    value typed: {step.value!r}")
        if step.url:
            bits.append(f"    url: {step.url}")
        if step.extract_as:
            bits.append(f"    extracted as: {step.extract_as}")
        lines.append("\n".join(bits))
    return "\n".join(lines)


def generalize(
    draft: Capability,
    outputs: dict[str, str],
    *,
    client: Any,
    model: str = DEFAULT_MODEL,
    goal: str | None = None,
) -> Capability:
    """Return a parameterised, contract-bearing copy of the draft."""
    response = client.messages.create(
        model=model,
        max_tokens=8_000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=[
            {
                "type": "text",
                "text": GENERALIZE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[EMIT_CAPABILITY_TOOL],
        messages=[
            {
                "role": "user",
                "content": generalize_message(
                    goal or draft.description, _steps_summary(draft), outputs
                )
                + "\n\nCall emit_capability now.",
            }
        ],
    )

    call = next((b for b in response.content if b.type == "tool_use"), None)
    if call is None:
        # Not fatal. The draft already replays; it is just not yet reusable,
        # so it stays a draft and a human can finish the contract by hand.
        return draft

    return apply_generalization(draft, call.input, outputs)


def apply_generalization(
    draft: Capability, spec: dict, outputs: dict[str, str]
) -> Capability:
    """Mechanically apply the model's analysis to the recorded steps."""
    cap = draft.model_copy(deep=True)

    cap.id = _slug(spec.get("id") or cap.id)
    cap.name = spec.get("name") or cap.name
    cap.description = spec.get("description") or cap.description
    cap.version = "1.0.0"
    cap.status = "draft"  # promotion to `approved` is a deliberate human act

    cap.risk = Risk(
        **{"class": spec.get("risk_class", "read_only")},
        requires_approval=spec.get("risk_class") == "irreversible",
        rationale=spec.get("risk_rationale", ""),
    )

    # --- parameters: replace the literals typed during the run -------------
    inputs: list[InputParam] = []
    for param in spec.get("parameters", []):
        literal = str(param.get("literal_value", ""))
        if not literal:
            continue
        placeholder = "{{" + param["name"] + "}}"
        replaced = False
        for step in cap.steps:
            if step.value is not None and step.value.strip() == literal.strip():
                step.value = placeholder
                replaced = True
            if step.url and literal in step.url:
                step.url = step.url.replace(literal, placeholder)
                replaced = True
        if not replaced:
            # The model proposed a parameter we never actually typed. Dropping
            # it is correct: an input that reaches no step is a lie in the
            # capability's contract.
            continue
        inputs.append(
            InputParam(
                name=param["name"],
                type=param.get("type", "string"),
                required=True,
                description=param.get("description", ""),
                pattern=param.get("pattern") or None,
                example=literal,
                sensitivity=param.get("sensitivity", "internal"),
            )
        )
    cap.inputs = inputs

    # --- scrub this run's data out of everything a reviewer reads ----------
    # The model writes intents in the words of the run ("look up member
    # 12345"), and the captured HTML snippet of a value cell *is* the value.
    # Parameters become placeholders; anything else the run saw is redacted.
    literals = {p.example: p.name for p in inputs if p.example}
    scrub = Redactor(set(literals) | {v for v in outputs.values() if v}, strict=True)
    for step in cap.steps:
        for literal, name in literals.items():
            step.intent = step.intent.replace(literal, "{{" + name + "}}")
        step.intent = scrub.text(step.intent)
        captured = step.target.captured if step.target else None
        if captured and captured.recorded_html_snippet:
            captured.recorded_html_snippet = scrub.text(captured.recorded_html_snippet)
    by_literal = Redactor(set(literals))
    cap.name = by_literal.text(cap.name)
    cap.description = by_literal.text(cap.description)
    if cap.provenance and cap.provenance.goal:
        cap.provenance.goal = scrub.text(cap.provenance.goal)

    # --- outputs: only those that a real extract step produced -------------
    extract_steps = {s.extract_as: s.id for s in cap.steps if s.action == "extract"}
    declared: list[OutputSpec] = []
    for out in spec.get("outputs", []):
        step_id = extract_steps.get(out["name"])
        if step_id is None:
            continue
        declared.append(
            OutputSpec(
                name=out["name"],
                type=out.get("type", "string"),
                from_step=step_id,
                required=True,
                description=out.get("description", ""),
            )
        )
    for name, step_id in extract_steps.items():
        if name and not any(d.name == name for d in declared):
            declared.append(
                OutputSpec(
                    name=name,
                    type="string",
                    from_step=step_id,
                    required=True,
                    description=f"Value extracted at step {step_id}.",
                )
            )
    cap.outputs = declared

    # --- business outcomes -------------------------------------------------
    cap.outcomes = [
        BusinessOutcome(
            name=_slug(o["name"]),
            description=o.get("description", ""),
            detect=Condition(kind="text_present", text=o["detect_text"]),
        )
        for o in spec.get("outcomes", [])
        if o.get("detect_text")
    ]

    return cap


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in (text or "capability"))
    return "_".join(p for p in out.split("_") if p) or "capability"
