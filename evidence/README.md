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

Real runs against the live app: `gpt-4.1`, via
`cu discover --provider openai --goal "look up member 12345 and read their current savings balance"`.

| directory | what happened |
|---|---|
| `discovery_20260914T162654_3538c9` | **First live run — escalated.** The model typed the member number and searched, then called `escalate`: it could see the balance, but the observation numbered only controls, so there was nothing to `extract` from. Stuck detection working as designed, over a real defect ([REPORT §7](../REPORT.md#what-the-first-live-run-found)). Its log was re-redacted afterwards, once the run had also shown that unclassified values reached disk. |
| `discovery_20260914T164432_eb67f9` | **Live run after the fixes — success** in 4 turns: type → click → extract → done. Produced [`capabilities/discovered/lookup_member_savings_balance@1.0.0.json`](../capabilities/discovered/). |
| `verify_20260914T164447_4cae22` | The verification replay `cu discover` runs before saving: `success`. |

One intermediate successful run is not here: review found its artifact still
carried the member number inside model-written intents, so that was fixed and
discovery run again. The artifact and the evidence above come from the same run.

Grep `model_decision` in a discovery log for each turn: the tool, the stated
intent, and the redacted arguments.

### Replaying what the model discovered

No model in the loop.

| directory | artifact | input | result |
|---|---|---|---|
| `replay_20260914T164511_b8423d` | 1.0.0 | 23456 — not the recorded member | `success`, `savings_balance` |
| `replay_20260914T164519_236f02` | 1.0.0 | 99999 | `failed` at `s2_click_search`; observed "No member record found" |
| `replay_20260914T164542_b3967d` | 1.0.0 | 55555 | `failed` at `s2_click_search`; observed "Access restricted" |
| `replay_20260914T164709_804a41` | 1.0.1 | 23456 | `success` |
| `replay_20260914T164717_6c6140` | 1.0.1 | 99999 | `business_outcome: member_not_found` |
| `replay_20260914T164722_08ae14` | 1.0.1 | 55555 | `business_outcome: permission_denied` |

**Read the 1.0.0 failures before the 1.0.1 outcomes.** Generalisation asked the
model to declare the business outcomes, and it declared plausible ones
("Member not found", "Access denied") for pages it had never seen — a
happy-path run cannot observe them. Replay did the safe thing: no detector
matched, so it did not guess, and it stopped with the real page text in
`observed`. `1.0.1` is the reviewer's correction: those observed texts copied
in, and a third invented outcome dropped because nobody has seen it fire. That
is the draft → review loop the `status` field exists for.
