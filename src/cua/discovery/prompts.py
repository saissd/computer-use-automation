"""Prompts and tool definitions for the discovery agent.

Three things shape this prompt.

**The model never sees a selector.** It acts on integer refs from a numbered
list of controls. That is not a convenience — it means the model structurally
cannot invent a locator, so every locator in the resulting artifact was
captured from a real element the harness resolved. Hallucinated selectors are
impossible rather than merely discouraged.

**Every action carries an `intent`.** That sentence becomes `Step.intent` in
the artifact. It is how a human reviewer later understands what the flow is
doing, and it moves the model's reasoning into the durable artifact instead of
leaving it in a transcript we throw away.

**The model is told to stop rather than improvise.** In a bank's back office
the expensive failure is not giving up, it is confidently doing the wrong
thing. The prompt says so, and the policy gate enforces it regardless.
"""

SYSTEM_PROMPT = """\
You are operating a bank's internal back-office web application the way a \
trained human teller would: by reading the screen and using the controls.

You are in DISCOVERY mode. You are working out how to accomplish a goal for \
the first time. Everything you do is being recorded into a reusable, \
deterministic automation (a "capability") that will later be replayed many \
times without you. Act accordingly: take the clean, repeatable path a careful \
operator would take, not a clever shortcut.

HOW YOU SEE THE APPLICATION
Each turn you receive the current state as:
  * a numbered list of interactable controls, each with an accessibility role \
and name, e.g.  [12] textbox "Member Number"
  * a numbered list of readable values — table cells showing data, with the \
text of their row for context, e.g.  [31] cell value='Active' row='Checking | Active'. \
These can only be used with `extract`.
  * the visible text of the page.
This is the same semantic view a screen reader exposes. You will not be given \
HTML, CSS selectors, or coordinates, and you do not need them.

HOW YOU ACT
Call exactly one tool per turn, addressing controls by their number.
Never invent a control number — only use numbers present in the CURRENT \
observation. Numbers change between turns; re-read the list each time.

Every tool call takes an `intent`: one short sentence, in operator language, \
saying what this step accomplishes ("Type the member number into the search \
field"). This sentence is saved into the automation and read by human \
reviewers, so write it for them.

RULES
1. One action per turn. After each action you will see the new state.
2. Prefer controls with meaningful names. If two controls look equally \
plausible, pick the one a human operator would obviously use.
3. Use `extract` for every value the goal asks you to report back, addressing \
the numbered readable value that holds it. Give each one a clear snake_case \
output name. A value you do not extract is not returned to the caller, even if \
you can see it in the visible text.
4. If a page shows an error, a warning, or an unexpected notice, read it \
before acting. Dismiss a harmless interstitial; do not click past a real error.
5. Never take an action that moves money, deletes a record, or is otherwise \
irreversible. If the goal appears to require one, call `escalate`.
6. If you are stuck, looping, or unsure whether an action is safe, call \
`escalate` rather than guessing. Stopping is a good outcome. Doing the wrong \
thing confidently is the expensive one.
7. When the goal is achieved and you are looking at the page that proves it, \
call `done`.

You are working in a training environment with synthetic data. Do not enter \
real personal data. Never type credentials into a form unless the goal \
explicitly provides them.
"""


def goal_message(goal: str, entry: str, allowlist: list[str]) -> str:
    return (
        f"GOAL: {goal}\n\n"
        f"ENTRY POINT: {entry}\n"
        f"PERMITTED HOSTS: {', '.join(allowlist)}\n\n"
        "The application is already open and you are signed in. "
        "Begin by reading the current state below, then take your first action."
    )


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    """Build a strict tool definition.

    `strict: true` plus `additionalProperties: false` means the arguments we
    receive are guaranteed to validate, so the loop never has to defensively
    parse a malformed tool call.
    """
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


_INTENT = {
    "type": "string",
    "description": "One short operator-language sentence describing what this "
    "step accomplishes. Saved into the automation and read by human reviewers.",
}
_REF = {
    "type": "integer",
    "description": "The number of a control in the CURRENT observation.",
}

TOOLS = [
    _tool(
        "click",
        "Click a control: a button, a link, or a checkbox.",
        {"ref": _REF, "intent": _INTENT},
        ["ref", "intent"],
    ),
    _tool(
        "type",
        "Type text into a text field. Clears the field first.",
        {
            "ref": _REF,
            "text": {"type": "string", "description": "The text to enter."},
            "intent": _INTENT,
        },
        ["ref", "text", "intent"],
    ),
    _tool(
        "select",
        "Choose an option in a dropdown by its visible label.",
        {
            "ref": _REF,
            "option": {"type": "string", "description": "The option label to choose."},
            "intent": _INTENT,
        },
        ["ref", "option", "intent"],
    ),
    _tool(
        "press",
        "Press a keyboard key, e.g. Enter or Tab.",
        {
            "key": {"type": "string", "description": "Key name, e.g. 'Enter'."},
            "intent": _INTENT,
        },
        ["key", "intent"],
    ),
    _tool(
        "extract",
        "Read a value off the screen and return it as a named output of the "
        "capability. Use this for every value the goal asks for. `ref` is "
        "normally a number from READABLE VALUES.",
        {
            "ref": _REF,
            "output_name": {
                "type": "string",
                "description": "snake_case name for this output, e.g. savings_balance.",
            },
            "intent": _INTENT,
        },
        ["ref", "output_name", "intent"],
    ),
    _tool(
        "escalate",
        "Stop and hand this to a human operator. Use when you are stuck, "
        "looping, blocked by a permission or policy, or when proceeding would "
        "be unsafe or irreversible.",
        {
            "reason": {
                "type": "string",
                "description": "What you tried, what you observed, and what a "
                "human needs to decide or do.",
            }
        },
        ["reason"],
    ),
    _tool(
        "done",
        "The goal is achieved and the current page proves it.",
        {
            "summary": {
                "type": "string",
                "description": "What was accomplished, in one or two sentences.",
            },
            "success_text": {
                "type": "string",
                "description": "A distinctive phrase visible on the CURRENT page "
                "that proves the goal was reached. This becomes the automation's "
                "success checkpoint, so choose something specific to this state "
                "and not present on earlier pages.",
            },
        },
        ["summary", "success_text"],
    ),
]


# --------------------------------------------------------------------------
# Generalisation pass
# --------------------------------------------------------------------------

GENERALIZE_SYSTEM = """\
You are turning a recorded UI automation into a reusable, parameterised \
capability that an AI agent will invoke by name with typed arguments.

The steps have already been captured mechanically and are correct. You are NOT \
rewriting them. You are doing four things only:

1. PARAMETERS. Identify which literal values typed during the run were really \
*inputs* that will differ per invocation (a member number, an amount), versus \
values that are fixed properties of the flow. For each input give a snake_case \
name, a type, a regex pattern if there is an obvious one, and a sensitivity: \
  - "pii" for anything identifying a member (member numbers, names, SSNs)
  - "secret" for credentials or tokens
  - "internal" for ordinary business values
  - "public" only for values that are genuinely not sensitive
When in doubt, classify higher. Over-redacting costs a debugging inconvenience; \
under-redacting leaks regulated data.

2. IDENTITY. A snake_case id, a short human title, and a description written \
for the AI agent that will call this capability — say what it does, what it \
needs, and what it returns.

3. OUTCOMES. Business outcomes are legitimate non-success answers the caller \
must be told about ("no such member", "permission denied", "validation \
failed"). They are NOT errors. For each, give a name and a distinctive phrase \
that appears on screen when it happens. Think about what this flow would \
plausibly hit in production even if it did not happen during this run.

4. RISK. Classify the capability:
  - "read_only": only reads data
  - "reversible_write": creates or edits something that can be undone
  - "irreversible": moves money, deletes records, sends notices, or otherwise \
cannot be taken back

Be conservative on risk and sensitivity. This runs unattended against a \
financial institution's systems.
"""


def generalize_message(goal: str, steps_summary: str, outputs: dict) -> str:
    return (
        f"GOAL THAT WAS ACHIEVED:\n{goal}\n\n"
        f"RECORDED STEPS:\n{steps_summary}\n\n"
        f"VALUES EXTRACTED DURING THE RUN:\n{outputs}\n\n"
        "Call emit_capability with your analysis."
    )


EMIT_CAPABILITY_TOOL = {
    "name": "emit_capability",
    "description": "Emit the parameterisation and contract for the recorded flow.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "snake_case capability id"},
            "name": {"type": "string", "description": "Short human-readable title"},
            "description": {
                "type": "string",
                "description": "Agent-facing description: what it does, needs, returns.",
            },
            "risk_class": {
                "type": "string",
                "enum": ["read_only", "reversible_write", "irreversible"],
            },
            "risk_rationale": {"type": "string"},
            "parameters": {
                "type": "array",
                "description": "Values typed during the run that are per-invocation inputs.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "literal_value": {
                            "type": "string",
                            "description": "The exact value typed during the run, so it "
                            "can be replaced with a placeholder.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["string", "number", "boolean", "money", "date"],
                        },
                        "description": {"type": "string"},
                        "pattern": {
                            "type": "string",
                            "description": "Regex the value must match, or empty string.",
                        },
                        "sensitivity": {
                            "type": "string",
                            "enum": ["public", "internal", "pii", "secret"],
                        },
                    },
                    "required": [
                        "name",
                        "literal_value",
                        "type",
                        "description",
                        "pattern",
                        "sensitivity",
                    ],
                    "additionalProperties": False,
                },
            },
            "outputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["string", "number", "boolean", "money", "date"],
                        },
                        "description": {"type": "string"},
                    },
                    "required": ["name", "type", "description"],
                    "additionalProperties": False,
                },
            },
            "outcomes": {
                "type": "array",
                "description": "Legitimate non-success results the caller needs.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "detect_text": {
                            "type": "string",
                            "description": "Distinctive on-screen phrase for this outcome.",
                        },
                    },
                    "required": ["name", "description", "detect_text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "id",
            "name",
            "description",
            "risk_class",
            "risk_rationale",
            "parameters",
            "outputs",
            "outcomes",
        ],
        "additionalProperties": False,
    },
}
