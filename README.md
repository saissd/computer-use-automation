# Computer-Use Automation System

An LLM works out how to drive a legacy back-office application once. What it
learned is recorded as a **typed, versioned capability artifact**. From then
on that artifact is replayed **deterministically, with no model in the loop** —
cheaply, repeatably, and with an explicit contract an AI agent can call.

> The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how the agent invokes it in production.

Design write-up: **[REPORT.md](REPORT.md)**. Evidence: **[evidence/](evidence/)**.

---

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
playwright install chromium
```

On Windows, clone into a short path (e.g. `C:\code\`). One file in the
`anthropic` SDK has a very long name, and a deep clone location pushes it past
the 260-character path limit, which makes `pip install` fail with an `OSError`.

Only `cu discover` needs a model API key. Everything else — replay, the error
suite, the human-handoff demo, the whole test suite — runs offline.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # default model: claude-opus-5
# or
export OPENAI_API_KEY=sk-...          # default model: gpt-4.1
```

Or copy `.env.example` to `.env` (gitignored) and put the key there.
`cu discover` picks whichever key is set; force one with `--provider openai`
and override the model with `--model`. Both providers run the identical loop:
OpenAI is adapted at the client boundary
([`discovery/llm_openai.py`](src/cua/discovery/llm_openai.py)), not in the
agent.

---

## Demo path

### 1. Start the target application

```bash
cu serve-app            # http://127.0.0.1:8099   (leave running)
```

A deliberately hostile stand-in for a credit union's core banking console:
HTML 4 frameset, table-based layout, `<font>` tags, ASP.NET-style control ids,
**no test IDs**, and injectable runtime failures. Sign in as
`teller01` / `training-only`. Try members `12345`, `23456`, `34567` — and
`99999` (not found) and `55555` (permission denied).

### 2. Run the agent on a goal

```bash
cu discover --goal "look up member 12345 and read their current savings balance"
```

The LLM drives the real UI through the accessibility tree, one action per
turn. Each action it takes is recorded as a typed step with a ranked,
multi-signal element descriptor captured from the live control. When it
finishes, a single generalisation pass parameterises the literals it typed,
names the capability, declares its outputs and business outcomes, and
classifies its risk. The artifact is then **replayed once to verify it** before
being saved to `capabilities/`.

Add `--headful` to watch it work.

The committed live run (`gpt-4.1`, evidence in
[evidence/](evidence/README.md#the-discovery-run)) was saved with
`--out capabilities/discovered`. Replay what the model discovered — with a
different member than it was recorded with:

```bash
cu replay lookup_member_savings_balance --catalog-dir capabilities/discovered --input member_number=23456
```

### 3. Replay the artifact deterministically

`capabilities/lookup_member_balance@1.0.0.json` is a hand-authored reference
artifact for the same flow; the error suite and handoff demo run against it.

```bash
cu replay lookup_member_balance --input member_id=12345
```

No model is called. Same inputs, same steps, same outputs.

```
──────────── replay lookup_member_balance@1.0.0  (approved) ─────────────
  step               status   tier      locator                      detail
  open_console       ok       -         -
  enter_member       ok       primary   role='textbox' name='Memb…'  typed [REDACTED:member_id:len=5]
  click_search       ok       primary   role='button' name='Search'
  extract_name       ok       primary   near='Member Name'           member_name='Dana Whitfield'
  extract_balance    ok       primary   near='Regular Savings'       savings_balance='$8412.55'

outputs
  member_name = 'Dana Whitfield'
  savings_balance = '$8412.55'
```

### 4. Watch it handle things going wrong

```bash
cu replay lookup_member_balance --input member_id=99999    # business outcome, not a crash
cu replay lookup_member_balance --input member_id=55555    # permission denied
cu replay lookup_member_balance --input member_id=abc      # rejected before touching the browser
```

The full taxonomy, with injected runtime conditions, is in the test suite and
in [evidence/](evidence/README.md).

### 5. Human-in-the-loop takeover

```bash
cu handoff-demo
```

Opens a real browser, forces a session expiry the automation cannot recover
from on its own, and routes an intervention to the operator console at
`http://127.0.0.1:8100`. Take control, sign in by hand **in that same browser
window**, hand control back, and the run resumes on that session and completes.

### 6. The agent-facing view

```bash
cu catalog                    # what capabilities exist, typed
cu catalog --tools            # function-calling schemas an agent would receive
cu invoke lookup_member_balance '{"member_id": "12345"}'
```

`cu invoke` is the shape an AI agent's tool layer uses: a name, typed JSON
arguments, and a structured JSON result — no browser knowledge required.

```json
{ "status": "business_outcome", "outputs": {},
  "outcome": "member_not_found",
  "outcome_detail": "No member record found\nMember number [REDACTED] does not exist…" }
```

---

## Tests

```bash
pytest -q          # 80 tests, ~4 minutes, no API key, no network
```

They run against the real app in a real browser, because the claims worth
testing ("an ambiguous descriptor is refused", "a session timeout is
re-authenticated rather than blindly retried") are claims about behaviour
under a real surface.

| file | covers |
|---|---|
| `test_replay_outcomes.py` | the full error taxonomy, one test per class |
| `test_locator.py` | primary vs fallback vs ambiguous resolution, frame scoping |
| `test_handoff.py` | escalation, lease transfer, resume, abort, lease expiry |
| `test_policy_and_redaction.py` | allowlist, risk classification, redaction, input contract |
| `test_discovery.py` | the discovery loop with a scripted model: descriptor capture, value extraction, no run data on disk, discovered artifact replays |
| `test_llm_openai.py` | the OpenAI backend's translation to and from the loop's message shape |

---

## Other commands

```bash
cu stability lookup_member_balance -i member_id=12345 -n 10   # flakiness signal
cu drift                                                      # which artifacts are decaying
cu schema --out capability.schema.json                        # the artifact JSON Schema
cu console                                                    # operator console alone
```

---

## Layout

```
apps/legacy_cu/      the hostile target application (frameset, no test IDs, injectable errors)
apps/operator/       operator console — intervention queue and control transfer
src/cua/
  core/              the artifact schema and the result contract
  surface/           Surface protocol | WebSurface (Playwright) | DesktopSurface (stub)
  discovery/         LLM loop, tool surface, descriptor capture, generalisation
  replay/            the deterministic interpreter
  policy/            allowlist, risk classification, redaction
  session/           browser session, lease, intervention queue, registry
  catalog/           capability store and the agent-facing tool schemas
config/
  policy.yaml              what the agent is permitted to do
  recovery_profiles.yaml   per-application runtime conditions every capability inherits
capabilities/        saved artifacts
evidence/            run logs, results, screenshots, traces
```

## Configuration

| file | purpose |
|---|---|
| `config/policy.yaml` | domain and route allowlist, permitted action types, irreversible-action hints. Default-deny. |
| `config/recovery_profiles.yaml` | app-level runtime conditions (session expiry, error pages, interstitials) inherited by every capability recorded against that app. |

No secrets in the repo. The target app's training credentials are defaults on
the CLI and can be overridden with `--user` / `--password`.
