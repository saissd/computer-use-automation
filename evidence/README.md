# Evidence

One directory per run. Regenerate the replay set with:

```bash
python scripts/make_evidence.py
```

Each directory contains:

| file | what it is |
|---|---|
| `run.jsonl` | append-only structured log — every step, why it was taken, which locator tier resolved it, what was extracted |
| `result.json` | the final `ReplayResult` returned to the caller |
| `screenshots/` | captured on failure and on escalation |
| `dom.html` | every frame's HTML, captured on hard failure |
| `trace.zip` | Playwright trace — open with `playwright show-trace trace.zip` |

Everything here passes through the redactor on the way to disk. The member id
is declared `pii` in the capability, so it appears as `[REDACTED:member_id:len=5]`
in logs and `[REDACTED]` in extracted page text. You will not find `12345`
anywhere in this directory — that is asserted by a test
(`test_pii_never_reaches_the_run_log`).

## The replay set

Same capability, same artifact, ten different runtime conditions. Read it as
the error taxonomy in action.

| run | condition | result | what it demonstrates |
|---|---|---|---|
| `replay_01_success` | member 12345 | `success` | happy path, typed outputs |
| `replay_02_success_other_member` | member 23456 | `success` | the artifact is genuinely parameterised |
| `replay_03_business_outcome_not_found` | member 99999 | `business_outcome` | **"no such member" is an answer, not a crash** |
| `replay_04_business_outcome_denied` | member 55555 | `business_outcome` | permission denial is distinct from not-found |
| `replay_05_recovered_interstitial` | maintenance modal injected | `success` | declared handler dismissed it and carried on |
| `replay_06_recovered_slow_load` | 8s stall injected | `success` | condition-based waits, not fixed sleeps |
| `replay_07_recovered_session_reauth` | session expired | `success` | re-authenticated and retried the step |
| `replay_08_hard_failure_app_error` | app 500 injected | `failed` | stops, with step + expected + observed + DOM + screenshot |
| `replay_09_invalid_input` | `member_id="not-a-member"` | `failed` | contract violation caught before the browser is touched |
| `replay_10_human_handoff` | session expired, no re-auth available | `success` | escalation → human takes the live session → resume |

### Worth opening first

**`replay_03`** and **`replay_08`** side by side. Both are "the page did not
show a member". One returns `business_outcome` with `outcome: member_not_found`
and exit status OK; the other returns `failed` with a debuggable error. The
difference is declared in the artifact, not guessed at runtime.

**`replay_10_human_handoff`** — grep `run.jsonl` for `escalation_raised`,
`human_handoff`, and `lease_history`. The `human_handoff` event records what
changed while the operator held the session:

```json
{
  "event": "human_handoff",
  "operator": "casey.operator",
  "resolution": "resume",
  "url_before": "http://127.0.0.1:8099/login",
  "url_after": "http://127.0.0.1:8099/",
  "elements_added": ["button:Search", "link:Member Search",
                     "link:Sign Off", "textbox:Member Number"]
}
```

Note the automation did not take the operator's word for what they did — it
diffed the accessibility tree across the handoff. The `lease_history` event in
the same file shows the two control transfers.

## The discovery run

`cu discover` writes its evidence here too, under `discovery_*`, alongside the
`verify_*` replay that proves the freshly recorded artifact actually works.

It needs a model API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
cu discover --goal "look up member 12345 and read their current savings balance"
```

The discovery log records each turn: the observation the model received, the
tool it called, its stated intent, which control it acted on, and the
descriptor that was captured from that control. Grep for `model_decision`.
