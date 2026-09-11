# Design write-up

The framing that drove every decision here: **this is a compiler, not a browser
agent.**

| compiler | this system |
|---|---|
| front-end | the LLM discovery run — natural-language goal → structured flow |
| **intermediate representation** | **the capability artifact** |
| back-end / VM | the deterministic replay engine |
| target triple | the surface driver (legacy web / modern web / desktop) |
| runtime exception handling | the error taxonomy and its declarative handlers |
| debug symbols | the evidence bundle |

Three consequences fall out of that framing rather than being bolted on. The
model/production boundary is structural — a compiler does not ship its
front-end into production, and no model is reachable from the replay path.
The IR must be surface-agnostic, which turns out to *be* the answer to the
heterogeneity question. And replay is an interpreter over that IR, so
supporting a new kind of application means writing a driver, not a second
system.

---

## 1. Architecture

Python 3.11, Playwright (async), FastAPI, Pydantic v2, Typer. Single process.

**Why single process, async.** The brief requires a human to take control of
*the same live session* the automation was using. If the operator console runs
elsewhere, "the same session" immediately means CDP-over-the-wire or VNC on day
one. Co-locating the console and the agent loop in one process, sharing one
`BrowserContext`, makes that requirement literally true — cookies, form state
and all — and leaves the remoting as a clean, later substitution behind
`session/registry.py`. The seam is explicit; the plumbing is not built.

**Why Pydantic for the schema.** The artifact has two audiences: the replay
engine, which needs runtime-validated types, and a human reviewer, who needs a
readable contract. `model_json_schema()` derives the second from the first, so
they cannot drift. `cu schema` emits it.

**Why JSON files, not a database.** The requirement on artifacts is that they
be *reviewable and versioned*. Git-diffable JSON is the most reviewable thing
available and the version control is free. A database would be ceremony at
this scale, and the brief explicitly discounts scaling infrastructure.

**Why a custom tool surface rather than Anthropic's `computer_use` tool.**
This is the decision I would defend hardest. The built-in computer-use tool is
screenshot-and-coordinates: excellent for open-ended exploration, and exactly
wrong here. Coordinates are the highest-fidelity description of *where*
something was and the least useful description of *what it was*. A discovery
run that emits coordinates produces nothing replayable. So the model instead
acts on integer refs into a numbered list of controls derived from the
accessibility tree — and therefore *structurally cannot* emit a locator at
all. Every locator in every artifact was captured by the harness from a real
element.

**The layering.** `core` (types, no dependencies) ← `surface` (perceive/act) ←
`replay` / `discovery` ← `cli`. `policy` and `session` cut across. The
dependency arrow never points back toward `core`, and nothing above `surface`
imports Playwright.

### The seam that matters

```python
class Surface(Protocol):
    async def observe(self, screenshot: bool = False) -> Observation
    async def resolve(self, descriptor: ElementDescriptor) -> Resolution
    async def act(self, action, resolved, **kwargs) -> ActResult
    async def wait_for(self, condition: Condition, timeout_ms: int) -> bool
    async def check(self, condition: Condition) -> bool
    async def snapshot_evidence(self, label: str) -> dict
```

Six methods. Everything above this line — the schema, the replay engine, the
policy gate, the escalation path — is written against it and knows nothing
about browsers.

### Trade-offs I made deliberately

**Eager element handles instead of Playwright Locators.** A Locator is lazy
and re-queries on use, which hides the one fact I most need: how many things
matched. Resolving eagerly to a list of handles makes ambiguity *observable*
and lets replay refuse to act. I lose Locator auto-waiting — but I wanted that
moved into the artifact as an explicit `WaitSpec` anyway, where it is declared
and reviewable rather than implicit in the driver.

**A per-observation `data-cua-ref` attribute is written into the page.** It
mutates the application under test, which I would not do against production.
It is confined to discovery (replay never uses refs) and is the cheapest way to
guarantee the element the model chose is the element the harness describes.

---

## 2. Artifact schema

Full types in [`src/cua/core/models.py`](src/cua/core/models.py); an example in
[`capabilities/`](capabilities/).

```jsonc
Capability {
  schema_version, id, name, version,       // semver; bump on any step change
  status: "draft" | "approved" | "deprecated",
  description,                             // the agent-facing tool description
  target:   { app_id, surface, entry, variant },
  inputs:   [{ name, type, required, pattern, example, sensitivity }],
  outputs:  [{ name, type, from_step, description }],
  risk:     { class, requires_approval, rationale },
  preconditions: [Condition],
  steps:    [Step],
  checkpoint: Condition,                   // the success condition
  outcomes: [BusinessOutcome],             // named non-failure results + detectors
  provenance: { discovered_by, model, goal, run_id, recorded_at, evidence_ref },
  stats:    { replays, successes, failures, fallback_resolutions, last_ok }
}
```

Five decisions in this shape are load-bearing.

**Element targeting is a ranked list of semantic signals, not a selector.**

```jsonc
ElementDescriptor {
  primary:   { by: "role_name", role: "textbox", name: "Member Number" },
  fallbacks: [ { by: "text_near", anchor: "Member Number", direction: "right" },
               { by: "css", selector: "#ctl00_ContentPlaceHolder1_txtMbrNo" } ],
  scope:     { frame_path: ["content"] },
  disambiguation: { nth, within_row_matching },
  captured:  { role, name, tag, bbox, recorded_html_snippet }
}
```

`role + accessible name` is primary because it is the vocabulary a screen
reader uses — and therefore the vocabulary shared with Windows UIA and macOS
AX. It survives CSS rewrites, framework upgrades and rebrands. CSS is last:
`ctl00_ContentPlaceHolder1_txtMbrNo` is stable *within one vendor build* and
meaningless across versions and tenants, which makes it a useful backstop and
a terrible primary. `text_near` exists because legacy table-layout forms have
controls with no accessible name at all — the label lives in the adjacent
`<td>` — and without it a large fraction of the real target population is
simply unaddressable.

**The error taxonomy lives in data, not in engine code.** `Step.on_error` is a
list of `{when: Condition, do: dismiss|retry|wait|reauth|escalate|return_outcome|fail}`.
A reviewer reads the JSON and sees exactly how this capability behaves when the
app misbehaves, and that behaviour is versioned with the artifact rather than
buried in a release of the runtime.

**Business outcomes are first-class and declared up front.** The brief names
conflating them with failures as the most common mistake, and it is a schema
problem before it is a code problem. If the artifact does not have somewhere to
say "no such member is a legitimate answer," the engine has no way to act on it.

**`sensitivity` on every input is load-bearing, not documentation.** Anything
above `internal` never reaches an artifact, a log, or the model's context.

**Provenance and stats.** This is regulated work: which model, which run, which
evidence bundle. `stats` feeds the drift report and the draft → approved gate.

`Capability.tool_schema()` renders any artifact as a function-calling
definition, so the catalog *is* the agent-facing surface with no second source
of truth. Referential integrity is enforced by validator — an output cannot
reference a step that does not extract it, a recovery cannot return an
undeclared outcome.

---

## 3. Determinism & error handling

### Determinism

Four rules, all testable:

1. **No model in the loop.** Fixed step order, fixed inputs.
2. **Waits are on conditions, never fixed sleeps.** A sleep is either too short
   under load or too slow every run.
3. **Locator resolution is unique-or-error.** If a descriptor matches two
   elements and declares no disambiguator, replay stops. It does not take the
   first. Taking the first match is how automation transacts against the wrong
   row of a member's accounts, and it fails *silently*, which is the worst
   available outcome. (`test_ambiguous_descriptor_refuses_to_guess`)
4. **Every step verifies its own post-state** before the next one runs.
   `test_replay_is_deterministic_across_runs` asserts two runs produce
   identical step logs and outputs.
5. **A failed post-assertion never re-executes a non-idempotent action.**
   Retrying a `navigate` or a `type` is free; retrying a `click` could submit
   the form twice, and in this domain the second submission opens a second
   account or moves money again. The wait has already given a slow page its
   chance; if the state is still wrong, replay stops and says so rather than
   pressing the button again.

Rules 4 and 5 came out of a bug the tests found, described below.

### Two bugs worth reporting

Writing `test_discovered_artifact_replays` — replay what discovery recorded,
but with a *different* member than it was recorded with — surfaced both.

The recorder had inferred this checkpoint for the search step:

```
text_present('Branch	Cedar Falls Main		Member Since	2011-03-14')
```

Perfectly true for member 12345 and false for every other member. My
contamination filter excluded values the run had *typed* and missed data that
merely *appeared*. The fix is blunt on purpose: a tab in `innerText` means a
table row, and a table row on a detail screen is record data; and a candidate
containing any digit is rejected outright. A checkpoint is worth nothing if it
is not true for every valid invocation, so a blunt filter that sometimes finds
no checkpoint beats a clever one that sometimes finds a wrong checkpoint.

The cascade it exposed was the more serious of the two. When that assertion
failed, replay retried the step — **re-clicking a submit button**. On a lookup
that is merely wasteful. On the sub-account flow the same app supports, it
would have opened two accounts. Hence rule 5. I would rather surface this than
quietly patch it: it is exactly the failure mode that makes UI automation
dangerous in a bank, and it was invisible until a test replayed a recording
with different data than it was recorded with.

### The taxonomy

The order in which a step's aftermath is judged is what keeps the classes from
collapsing into each other:

```
1. step.on_error handlers    most specific — declared on this step
2. capability.outcomes       global business detectors
3. step.post_assert          did this step do what it claimed
4. capability.checkpoint     did the flow reach the intended state
```

| class | example | response |
|---|---|---|
| **business outcome** | no such member, permission denied, validation failed | return `business_outcome`, **status OK** |
| **recoverable** | interstitial, spinner, transient slowness, stale element | declared handler, bounded attempts, continue |
| **session** | timeout / logged out | re-auth once and retry; escalate if unavailable |
| **hard failure** | checkpoint mismatch, app 500, element genuinely gone | stop; DOM + screenshot + trace; step/expected/observed |
| **policy** | out of allowlist, irreversible without approval | stop, `needs_human` with a ticket |

The result contract is four-way, not two:

```python
ReplayResult = success(outputs) | business_outcome(outcome, detail)
             | needs_human(intervention_id, reason) | failed(error)
```

`needs_human` being a *return value* rather than an exception is what stitches
replay to escalation: it is an outcome the caller must handle, not an error it
can swallow. `result.ok` is true for `business_outcome` — the automation
worked, the app just had a different answer — and a caller that retries against
a member who does not exist will loop forever.

The same page, under two different artifacts, is a business outcome or a hard
failure. Compare `evidence/replay_03` with `evidence/replay_08`. The
difference is declared by the artifact author, not guessed at runtime, and I
think that is the right place for it: only a person who understands the
business process knows whether "record not found" is an answer or an alarm.

### Drift

Secondary, per the brief, and it comes free. Every replay logs *which locator
tier* resolved each element. A capability that still passes but increasingly
needs fallbacks is decaying; `cu drift` surfaces it before it becomes an
incident. That is the difference between a scheduled re-record and a 3am page.

---

## 4. Heterogeneity & multi-tenant

### Surface abstraction

The seam is `Surface` (§1) and the artifact's refusal to contain anything
surface-specific — no selectors outside the last-resort CSS tier, no
coordinates ever, no `Page`.

For a **desktop app**, [`surface/desktop.py`](src/cua/surface/desktop.py) is a
documented stub with the UIA mapping written out:
`observe()` walks the UIA tree (`ControlType`→role, `Name`→name,
`BoundingRectangle`→bbox); `resolve()` turns a `role_name` signal directly into
`CreateAndCondition(ControlType == X, Name == Y)`; `act()` uses Invoke and
Value patterns rather than synthetic clicks. The `css` tier simply has no
analogue and is skipped — which is the correct degradation, not a failure.

The honest limit: **a web artifact is not automatically portable to a desktop
app.** Entry points and control names genuinely differ. What is portable is the
schema, the engine, the taxonomy, the policy gate and the escalation path. Only
the driver is rewritten, and `Surface` is its entire contract.

For a **legacy web app** the implementation already handles the hard parts —
frameset traversal via `Scope.frame_path`, name computation that understands
`title=` attributes and adjacent-cell labels, and `text_near` for controls with
no accessible name. Those are not hypothetical: the target app in this repo has
all three, and the `Initial Deposit` field has no label, no title and no
placeholder specifically to force that path.

### Multi-tenant reuse

Hundreds of tenants, ~20 apps each, many on the same vendor product configured
differently. Re-recording per tenant is the thing to avoid.

**Represent one capability as a base artifact plus per-tenant overlays.** The
base is recorded once against a reference install and keyed by
`target.app_id` — the *vendor product*, not the institution. An overlay is a
sparse document that may override the entry URL, override individual step
descriptors **by step id**, or add/skip steps. Resolution merges base →
version variant → tenant at load time, and the merged artifact is hashed so an
audit log can prove exactly what ran. `target.variant` already carries the
discriminator and step ids are already stable, so the schema supports this
today; the resolver is the piece I did not build.

**Drift detection is the same mechanism as §3, read per tenant.** Every replay
records which locator tier resolved. A tenant whose fallback rate is climbing
has a UI that has moved. That gives a cheap, quantitative health signal across
thousands of app instances without probing any of them, and it tells you
*which* tenants need a re-record rather than forcing a fleet-wide one.

**Application-level conditions are configuration, not discovery.**
[`config/recovery_profiles.yaml`](config/recovery_profiles.yaml) declares, per
`app_id`, what a session timeout and an error page look like. Every capability
recorded against that app inherits them. A discovery run sees one happy path
and cannot learn these — they did not occur — and rediscovering them per
capability would be unreliable and inconsistent. Learning an app's failure
modes once and sharing them across every automation is also just how a real
integration team works.

---

## 5. Escalation & handoff

### Detecting stuck

Explicit signals, not emergent behaviour. In discovery: observation hash
unchanged across 3 actions; the same action twice with no effect; the model
calling `escalate`; a policy denial on a required action; step or time budget
exhausted. In replay: any declared `escalate` handler, a policy block, an
approval requirement, or re-auth being unavailable.

Both producers emit the *same* `InterventionRequest`, carrying which capability
and goal, which step and its intent, the session id, a screenshot, a redacted
observation summary, and redacted inputs. One type, two producers, one console.

### Taking control: the lease

```python
SessionLease { owner: "automation" | "human", lease_id, holder, since, expires_at }
```

The automation calls `lease.assert_automation()` **before every action**, and
it *raises*. This is the difference between a real control-transfer model and a
flag consulted out of courtesy: once the lease moves to a human, the automation
physically cannot act, even if a step is queued and a timer fires.
(`test_human_takes_over_live_session_and_hands_back` asserts the raise.)

Leases expire. An operator who takes control and goes to lunch would otherwise
wedge a run forever; on expiry the lease reverts and the event is recorded.

### Two escalation modes, because callers differ

`return` is the default and what a production agent gets: raise the request,
persist the context, return a ticket immediately, hold no worker. `wait` parks
on the live session for interactive use. Blocking a worker for the length of a
coffee break looks fine in a demo and falls over at scale, so blocking is
opt-in. A `wait` that times out returns `needs_human` — **silence is never
consent**, particularly for anything irreversible.

### Handing back, and evidence

On resume the run continues on the same session from the step that stopped
(`completed_manually` skips it instead). Across the handoff the system diffs
the accessibility tree and records what actually changed — independent of what
the operator says they did. In `evidence/replay_10_human_handoff` the URL is
*identical* before and after, while `elements_added` shows
`["button:Search", "textbox:Member Number", …]`. The address bar would have
told you nothing; the tree diff proves the operator got back in. That diff is
also the natural seed for a future version that proposes the missing steps back
into the artifact.

**Mocked:** the pixels. A production console would embed the live session via
CDP screencast or noVNC against a containerised browser. Here the browser runs
headful on the same machine and the operator drives the real window. That is a
cut in the *presentation* of the session, not in the control model.

---

## 6. Safety

**Allowlist, enforced at the action layer.** Default-deny domains, routes and
action types in [`config/policy.yaml`](config/policy.yaml), checked between the
model's decision and the browser call. A prompt that says "stay on this domain"
is a suggestion; this is a control. It is also the only real mitigation for
prompt injection from page content: a malicious page can talk the model into
*deciding* anything, and none of it reaches the surface.

Writing the tests found a genuine bug in my own gate. `fnmatch`'s `*` spans
`/`, so `/member/*` admitted `/member/1/../../_control/inject` — the allowlist
appeared to hold while the request went elsewhere. Paths are now normalised
before matching. I mention it because it is exactly the class of failure that
makes a security control worse than none: it reads as protection.

**Risk is asymmetric on purpose.** `read_only` runs freely; `reversible_write`
runs and is flagged; `irreversible` is never executed unattended — blocked and
routed to a human, even mid-replay, even though that means a capability can
stop half-finished. The cost of a wrong irreversible action is unbounded; the
cost of a stalled run is a phone call. The authoritative signal is
`Capability.risk`, declared on the artifact and reviewed by a human; the
label-text heuristic ("Transfer", "Delete", "Close Account") is a backstop for
discovery, where no declaration exists yet.

**Redaction in two layers.** At capture, anything declared `pii` or `secret`
becomes a `{{placeholder}}` and its value never enters an artifact, log or
model context — password field values are not even read during observation. At
egress, a pattern scrub (SSN, PAN, long account numbers, email) on everything
headed for disk. The ordering matters for the same reason input validation
beats output escaping: the earlier you stop the data, the fewer places you have
to be right. `test_pii_never_reaches_the_run_log` asserts the member id appears
nowhere in the evidence.

### Limits, stated plainly

- **Pattern redaction is incomplete by construction.** It is the second layer
  precisely because it cannot be trusted as the first.
- **Screenshots can leak PII we did not anticipate.** Field masking mitigates;
  it does not eliminate. The real control is a synthetic-data sandbox.
- **The model sees page content during discovery.** Inherent to computer use.
  Same mitigation.
- **Label-based risk classification is a heuristic** and will mislabel some
  controls. It is a backstop behind an explicit declaration, not the control.
- **No authentication on the operator console.** It binds to localhost and is a
  demo surface; a real one sits behind the institution's SSO and takes operator
  identity from it rather than a text box.
- **`data-cua-ref` mutates the page under test** during discovery.

---

## 7. Cuts

Decided up front and documented, rather than discovered by running out of time.

| cut | what exists instead | why |
|---|---|---|
| **Desktop surface** | `Surface` protocol + a stub with the full UIA mapping written out | The seam is the deliverable; the driver is mechanical. Writing the stub proves the abstraction is not secretly web-shaped. |
| **Remote co-browsing** | Real lease, real queue, real evidence, real resume — headful takeover on the same machine | The control-transfer model is what is graded. Screencast plumbing is substitution behind `session/registry.py`. |
| **Multi-tenant overlay resolver** | Schema support (`target.variant`, stable step ids) and the drift signal | The brief explicitly discounts building scaling infrastructure. The design is in §4; the resolver is ~100 lines when a second tenant exists. |
| **Login inside the artifact** | Environment concern; credentials from env, `reauth` hook on the session | Baking auth into a recorded flow means re-recording per tenant and puts secrets one serialization from disk. |
| **A second `open_subaccount` capability** | The app fully supports the flow (4-field form, validation, confirmation) | Depth over breadth. It would exercise `reversible_write` risk and a validation business outcome, but re-uses machinery already proven. |
| **Assisted LLM fallback on replay failure** | Hard failure with full evidence | Attractive, and the wrong default for a bank. A bounded, policy-checked single-step recovery is the right version of it — see below. |

### What I would build next, in order

1. **The overlay resolver and a second app variant.** Highest value: it turns
   the multi-tenant story from a design into a demonstration, and the schema
   already carries it.
2. **A real remote console** — CDP screencast with input forwarding. The lease
   model does not change; only the transport.
3. **Confidence gating.** `stats` already accumulates. Promote draft → approved
   automatically after N clean replays, and refuse unattended invocation of a
   draft.
4. **Learning from handoffs.** The accessibility-tree diff across a human
   takeover already describes what they did. Proposing those as steps back into
   the artifact closes the loop: every escalation makes the capability better
   rather than merely unblocking one run.
5. **Bounded assisted recovery.** On a hard failure, allow the model *one*
   policy-checked step, recorded as evidence and requiring approval before it is
   written back. Never open-ended.

### One honest gap

`evidence/` contains ten replay runs covering the full taxonomy, all
reproducible offline via `scripts/make_evidence.py`. It does **not** yet contain
a discovery run, because the machine this was built on had no model API key.
The discovery loop is complete and is the code path `cu discover` exercises;
producing the evidence is one command with a key set:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
cu discover --goal "look up member 12345 and read their current savings balance"
```

It runs the loop, generalises, replays the result to verify it, and writes both
the artifact and the evidence. I would rather say this plainly than imply a run
happened that did not.
