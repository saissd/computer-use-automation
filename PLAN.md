# PLAN — Computer-Use Automation System

Working plan. `REPORT.md` is the deliverable write-up; this is the build doc.

## 0. Thesis

**This is a compiler, not a browser agent.**

| Compiler | Us |
|---|---|
| Front-end | LLM discovery run (NL goal → structured flow) |
| **IR** | **Capability artifact** ← the deliverable |
| Back-end / VM | Deterministic replay engine |
| Target triple | Surface driver (web / legacy web / desktop) |
| Runtime exceptions | Error taxonomy + declarative recovery handlers |
| Debug symbols | Evidence bundle |

Consequences: the LLM/production boundary is structural, the artifact must be
surface-agnostic (that IS the heterogeneity answer), and replay is an interpreter
with pluggable backends.

## 1. Locked decisions

| Area | Decision | Rationale |
|---|---|---|
| Language | Python 3.11+ | User call |
| Browser | Playwright **async** | Shared BrowserContext across agent + console |
| Web framework | FastAPI + Jinja2 (both apps) | One dep set; console must serve HTTP while agent runs |
| Schema | Pydantic v2 → `model_json_schema()` | Typed runtime + reviewable JSON Schema from one source |
| CLI | Typer | Type-hint native, pairs with pydantic |
| Model | `claude-opus-5`, adaptive thinking, `effort: xhigh` | Long-horizon agentic; no `budget_tokens` (400s on Opus 5) |
| Agent loop | Manual loop, custom tools, `strict: true` | Need a policy gate + evidence write on every turn |
| Perception | **Accessibility tree primary**, screenshot secondary | Only option with a real desktop story; survives CSS churn |
| NOT using | Anthropic built-in `computer_use` tool | Emits pixel coords — unreplayable in an artifact |
| Storage | JSON files on disk | Requirement is *reviewable*; git-diffable beats a DB here |
| Target app | Hostile local app + variant-B twin (if time) | Only way to inject the errors the brief grades |
| Handoff | Session lease + headful takeover | Mechanism real; pixels documented as prod path |
| Process model | **Single process, async** | "Same live session" becomes literal, not hand-waved |

## 2. Why the accessibility tree

| Option | Replay determinism | No clean DOM | Desktop | Tokens |
|---|---|---|---|---|
| Raw DOM | brittle selectors | no | no | huge |
| Screenshot + coords | **coords unreplayable** | yes | yes | high |
| **A11y tree** | role+name is semantic | yes | **UIA / AX** | compact |

`role="textbox", name="Member Number"` survives a rebrand and a framework upgrade.
`#ctl00_ContentPlaceHolder1_txtMbrNo` does not. It is also what a screen reader
sees — the same abstraction a human operator uses.

Model acts on `ref: 47` from a numbered interactables list. It never sees or writes
a selector, so it cannot hallucinate one. The harness captures the descriptor.

## 3. The artifact must NOT be written by the LLM at the end

1. **Accrete during the run** — each successful action appends a typed `Step` with a
   mechanically captured descriptor. Deterministic code, not generation.
2. **One narrow generalization pass** (structured output) does only: parameterize
   literals (`"12345"` → `{{member_id}}`), name/describe/declare outputs, propose checkpoint.
3. **Validate against JSON Schema, then immediately replay** as a self-check before
   declaring the recording successful.

Asking a model to reconstruct 14 steps from memory produces plausible fiction.

## 4. Schema sketch

```jsonc
Capability {
  schema_version, id, name, version,          // semver, diffable
  status: "draft"|"approved"|"deprecated",
  description,                                 // agent-facing
  target: { app_id, surface, entry, variant? },
  inputs:  [{ name, type, required, pattern?, sensitivity }],
  outputs: [{ name, type, from_step, extraction }],
  risk: { class: "read_only"|"reversible_write"|"irreversible", requires_approval },
  steps: [Step],
  checkpoint: Assertion,
  outcomes: [BusinessOutcome],                 // named non-failures + detectors
  provenance: { model, run_id, recorded_at, evidence_ref },
  stats: { replays, successes, last_ok }
}

Step {
  id, intent, action, target?: ElementDescriptor,
  value?: literal | "{{param}}",
  pre_wait?, post_assert?,                     // never assume a click worked
  on_error?: [Recovery],                       // taxonomy lives in DATA
  timeout_ms, retries
}

ElementDescriptor {                            // ranked, multi-signal
  primary:   { by: "role+name", role, name },  // portable to desktop
  fallbacks: [ {by:"label"}, {by:"text_near"}, {by:"css"} ],
  scope?:    { frame_path, container },        // frameset traversal
  disambiguation: { nth?, within_row_matching? },
  captured:  { role, name, tag, bbox, crop_ref }
}
```

Two points to defend:
- `on_error` is **data, not code** — a reviewer reads the JSON and sees how the
  capability behaves when the app misbehaves. Versioned with the artifact.
- Fallback tier used is **logged on every replay** → free drift detector (below).

## 5. Result contract — four-way

```python
ReplayResult =
  | success          (outputs, evidence, timing)
  | business_outcome (outcome, detail, evidence)   # exit 0, NOT a crash
  | needs_human      (intervention_id, reason, step_id, evidence)
  | failed           (error{class, step_id, expected, observed}, evidence)
```

`needs_human` as a first-class return is what stitches replay to escalation.

| Class | Example | Response |
|---|---|---|
| Business outcome | no such member, permission denied, validation error | return, exit 0 |
| Recoverable | interstitial, spinner, stale element, slow load | declared handler, bounded |
| Session | timeout / logged out | re-auth once → retry, else escalate |
| Hard failure | checkpoint mismatch, element gone, 500 | stop, full evidence |
| Policy | out-of-allowlist, irreversible w/o approval | stop, needs_human |

## 6. Determinism levers

- No LLM in the loop; fixed step order.
- Waits on **conditions**, never `sleep()`.
- **Locator resolution is unique-or-error.** Two matches, no disambiguator → hard fail.
  Never guess. This is the rule most implementations get wrong.
- Every step asserts post-state before advancing.
- Verify: replay twice, diff step logs modulo timestamps → byte-identical.

## 7. Stuck detection (feeds escalation)

| Signal | Threshold |
|---|---|
| Observation hash unchanged | 3 consecutive actions |
| Identical action repeated | 2× |
| Model calls `escalate` | immediate |
| Policy denial on required action | immediate |
| Step / wall-clock budget exceeded | configured |

All raise the same typed `InterventionRequest`. One escalation path, two producers
(discovery and replay).

## 8. Control transfer — session lease

```python
SessionLease { owner: "automation"|"human", lease_id, holder, since, expires_at }
```

- Automation checks the lease **before every action** and hard-stops without it. Not advisory.
- Take control acquires; hand back releases + records `HumanActionLog`.
- Lease timeout so a walked-away operator cannot wedge the run.
- Capture an **a11y-snapshot diff across the human's turn** — objective evidence of what
  changed, better than a self-report, and the seed for auto-learning missing steps.

## 9. Target app — hostile by design

`apps/legacy_cu/` — FastAPI + Jinja2, server-rendered.
Frameset (nav + content), table layout, `<font>` tags, `ctl00_...` ids, **zero test IDs**.

Flow: login → member search → member detail (savings balance) → open sub-account
(4-field form) → confirm → confirmation screen.

Error injection (the reason we build rather than borrow):

| Trigger | Condition |
|---|---|
| member `99999` | record not found |
| member `55555` | permission denied |
| amount > limit | field-level validation error |
| `?inject=slow` | 8s load |
| `?inject=500` | app error |
| `?inject=dialog` | surprise maintenance interstitial |
| idle > 60s | session timeout |

## 10. Safety

Allowlist enforced at the **action layer**, not the prompt layer (prompts are unenforceable):

```yaml
domains: [localhost:3000]
routes:  [/member/*, /account/*]
actions: {allowed: [click, type, select, read, navigate], denied: [download, upload, file_dialog]}
```

Risk: `read_only` auto-allow / `reversible_write` allow+log+flag / `irreversible`
**block + escalate, never auto-execute** unless `requires_approval` satisfied.

Redaction, two layers:
1. At capture — `sensitivity: pii|secret` values never enter artifacts, logs, or model context.
2. At egress — regex scrub (SSN, PAN, account no.) on every log write; screenshot field masking.

Stated limits: screenshots can leak unanticipated PII; the model sees page content during
discovery (inherent — mitigated by synthetic-data sandbox); prompt injection from page
content is real, and the mitigation that works is that **the policy gate lives outside
the model**.

## 11. Multi-tenant (design only, build nothing)

`base` + `variant` + `tenant` overlay, merged at load, merged artifact hashed for audit.
Overlay may override entry URL, override step descriptors **by step id**, add/skip steps.

**Drift detection is free:** every replay logs which locator tier resolved. Rising
fallback rate on a tenant = drifting UI → flag for re-record before it breaks.

## 12. Repo layout

```
apps/legacy_cu/        hostile target app (+ variant_b)
apps/operator/         intervention console
src/cua/
  core/                pydantic models: Capability, Step, Result, Policy
  surface/             Surface protocol | WebSurface (Playwright) | DesktopSurface (stub)
  discovery/           LLM loop, prompts, tools, accretion, generalization pass
  replay/              interpreter, locator resolver, waits, recovery, extraction
  policy/              allowlist, risk classification, redaction
  session/             lease, control transfer, intervention queue
  catalog/             agent-invocable capability catalog (stretch)
  cli.py               cu discover | replay | catalog | operator
tests/
capabilities/          saved artifacts
evidence/<run_id>/     run.jsonl, steps/NN.png, final.html, trace.zip, result.json
```

`Surface` is the heterogeneity seam:

```python
class Surface(Protocol):
    async def observe(self) -> Observation: ...          # a11y tree + screenshot + context
    async def resolve(self, d: ElementDescriptor) -> Handle | Ambiguous | NotFound: ...
    async def act(self, a: Action, h: Handle | None) -> ActResult: ...
    async def wait_for(self, c: Condition, ms: int) -> bool: ...
    async def snapshot_evidence(self) -> Evidence: ...
    lease: SessionLease
```

## 13. Build order

| Phase | Work | Why here |
|---|---|---|
| **P0** | core models, target app, replay engine, hand-authored artifact, policy, evidence | **Demo works with zero LLM.** De-risks everything |
| **P1** | discovery loop → real LLM run → real artifact → replay it | The one non-negotiable |
| **P2** | session lease, intervention queue, operator console | The differentiator most submissions TODO |
| **P3** | error-path suite, stability ×10, catalog | Evidence deliverable |
| **P4** | REPORT.md, README.md, evidence curation | Most-weighted deliverable — do not leave 30 min |

Timebox ~3 focused days. D1 = P0. D2 = P1+P2. D3 = P3+P4.

## 14. Test plan

Unit: locator resolver (primary wins / fallback fires / **ambiguity errors**),
param substitution + injection safety, redaction (property test), policy matrix.

Golden: hand-authored artifact + local app → exact outputs. No LLM, no network. CI.

Error suite — one replay per taxonomy row (99999, 55555, dialog, slow, timeout,
over-limit, 500, blocked nav) each asserting the right `ReplayResult` variant.

Stability: happy path ×10 → pass rate + p50/p95.

## 15. Cut lines (decided now, documented in REPORT §7)

| Component | Decision |
|---|---|
| Desktop surface | Protocol + stub raising `NotImplementedError`. Documented seam. Build nothing. |
| Operator console | Real lease, real queue, real evidence. Headful takeover. Not co-browsing pixels. |
| Multi-tenant | Overlay schema only. No registry, no DB, no plumbing. |
| Auth | Trivial login on local app; creds from env, never in artifact. |
| Stretch goals | At most one. Leading candidate: capability catalog (~50 lines, proves the agent-invocable thesis). |
