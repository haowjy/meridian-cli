# Design: The Spawn "Done" Model — observe outstanding work, don't infer quiescence

> Companion to `design.md` (managed-backend lifecycle ownership). That document
> owns **Layer A**: how a managed backend process is launched, supervised for
> liveness, and torn down without orphans. This document owns **Layer B**: *when
> is a spawn finished?* — the completion-detection model. The two layers compose;
> this one does not relitigate launch/liveness, it only changes how "done" is
> decided.

## 0. TL;DR

- **Instruct for the happy path; observe for correctness.** We *do* instruct the
  model to call `meridian spawn done` when finished and to write its report — that
  makes the common case clean and fast. But models are random: they don't reliably
  call `wait`/`done` or write a clean report even when told. So **correctness and
  report capture must hold if the model complies with nothing.** Instruction is an
  optimization layered on an observed-authority floor, never the floor itself.
- **No harness is exec-style run-to-exit.** Codex (`codex app-server`), OpenCode
  (`opencode serve`), Claude-headless (stdio), and Pi are all resident/event-
  driven. "Done" is always an observed condition, never a process exit.
- **A terminal turn event is NOT "done."** `turn/completed` (Codex) /
  `session.idle` (OpenCode) / `result` (Claude) / Pi quiescence are **turn
  boundaries**. The model can end its turn while parked on background work it
  fired (`meridian spawn --bg`, a backgrounded shell job). Reaping on the turn
  event is exactly the orphan/early-finalize bug — and it is **harness-agnostic**,
  not Pi-specific. Pi is just the only harness that currently *tracks* the
  outstanding work, so it's the only one that doesn't reap early today.
- **Completion is an idle-nudge loop with a deadline floor:**
  ```
  on natural idle:   nudge now + every ~5 min, cache-TTL-aligned (non-blocking): "if done, write your
                     report, then run `meridian spawn done`"
  child completes:   resume the model so it can react
  DONE when:         observed `meridian spawn done`            [primary]
                     OR deadline expired                       [zero-cooperation floor]
                     OR tracker proves nothing outstanding     [optional latency opt]
  ```
  The nudge solicits a clean done in the common case; the deadline guarantees
  termination when the model ignores it.
- **The load-bearing investment is the outstanding-work tracker, not a command.**
  Child spawns are *already* recorded harness-agnostically in the core spawn store
  (`parent_id`, `spawn_children`/`_collect_descendants`). So "does this parent have
  live children?" works for Codex/OpenCode too — no Pi disk-watcher. The only
  Pi-only extra is background bash.
- **What dies is the *inference*, not the *tracking*:** the 55-min ping, the
  quiescence-idle heuristic, the notification-timestamp gate, the micro-drain,
  and the Pi disk-watcher's child correlation (redundant with the core store).
- **Reaping is always the unsandboxed runner's job** — stop the backend/Pi
  process, terminate the recorded `ProcessScopeSnapshot`, terminate tracked
  subspawns. The model never needs `~/.meridian` write access. This is why the
  optional `meridian spawn done` works even when a sandbox would block the model
  from touching state.

## 1. The matrix (evidence-backed, from explorers p3861/p3862/p3863)

| Harness | Termination model | Turn-boundary event | Tracks outstanding bg work? | Bg work that outlives a turn? | Runner outside sandbox? |
|---|---|---|---|---|---|
| **Codex** | resident `codex app-server`, meridian stops it after the turn | `turn/completed` (`semantics.py:41-67`) | **No** (only core spawn store knows about child spawns; `&` shell jobs untracked) | **Yes, in practice** — model can fire `meridian spawn --bg` / bg shell, then end its turn | Yes — `execute_bg` worker is parent; app-server is its sandboxed child |
| **OpenCode** | resident `opencode serve`, meridian stops it after `session.idle` | `session.idle` (`semantics.py:122-137`) | **No** (same as Codex) | **Yes, in practice** | Yes |
| **Claude (headless)** | stdio NDJSON; **denied by default** (`deny_headless_harnesses=("claude",)`) | `result` + EOF (`semantics.py:77-100`) | **No**; no `ProcessScopeSnapshot` (PID only) | Possible; untracked | Yes |
| **Claude (primary)** | human-driven TUI | human exits | n/a | n/a | n/a — **not a subagent** |
| **Pi** | resident, quiescence-driven; `agent_end` does **not** finish it | quiescence inference (idle + no pending child/bash) + 55-min ping + child-wave (300s) + spawn-watch follow-ups | **Yes** — managed-bash + spawn-watch (and core store) | **Yes** — tracked background bash + child spawns (`disk_watcher.py:224-253`) | Split: Python runner outside, meridian-owned TS extensions run *inside* Pi |

### The two load-bearing assumptions, resolved

- **A. "No subagents run on headless Claude (it's blocked)." → TRUE by default.**
  `deny_headless_harnesses=("claude",)` enforced at `SPAWN_PREPARE`
  (`context.py:1734-1757`). A subagent whose model routes to Claude (`sonnet`) is
  rejected before launch. Configurable, but the default holds.
- **B. "Subagents run-to-exit and leave NO background work." → FALSE for every
  harness.** The difference between Pi and the others is **not** whether the
  "parked on background work" state can happen — it can happen on all of them —
  but only whether meridian *tracks* it today. Pi tracks it (and so doesn't reap
  early); Codex/OpenCode/Claude don't track it (and so reap at the turn event).

**Conclusion (corrected):** the "stopped, waiting on a process" state is
**harness-agnostic**. The completion fix is therefore a harness-agnostic
**outstanding-work tracker**, not a Pi quiescence special case. Pi's value is its
tracking; Pi's cost is its *inference* (ping/quiescence-gate/micro-drain), which
we delete.

## 2. The completion model (one loop, all resident harnesses)

```
on natural idle (turn ended on its own — NOT because meridian blocked it):
    stay resident; run the idle-nudge loop (§3.1):
       nudge now + every cadence interval (~5 min, cache-TTL-aligned; non-blocking follow-up turn):
         "if done, write your report as your final message, then run
          `meridian spawn done`"
on a tracked child completing:
    resume the model (re-prompt) so it can react to the result
parent is DONE when:
    observed `meridian spawn done`     → reap the whole tracked tree     [primary]
    OR deadline expired (hard cap)     → reap regardless                 [backstop]
    OR (optional) tracker proves: turn-ended ∧ no live children ∧ no running bash
                                       → fast-finalize without nudging    [latency opt]
reaping = process + scope + subspawns, by the unsandboxed runner
```

The **primary** path is the idle-nudge → model declares done. The **deadline**
guarantees termination when the model ignores every nudge (zero-cooperation
floor). The **optional fast-finalize** lets the tracker reap promptly when it can
*prove* there's nothing outstanding, instead of waiting for a nudge response.

Report capture follows the same instruct/observe split (see §4.1): the model is
instructed to write its report, but meridian captures it from the observed stream
regardless of compliance.

### Why each clause is forced by your constraints

- **The idle-nudge loop is the primary path.** On natural idle we persistently ask
  the model to declare done (non-blocking, ~5-min cadence). This solicits a clean,
  well-reported, prompt done in the common case — without inferring quiescence.
- **The deadline is the zero-cooperation floor.** Because models are random, the
  system must finalize even if every nudge is ignored. The deadline is a hard cap,
  not a quiescence timer. This is what makes correctness independent of the model.
- **Resume-on-child-completion stays.** Because models don't reliably `wait`, the
  only way a parent reacts to a child's result is for meridian to wake it — the
  existing Pi `spawn-watch` `triggerTurn:true` follow-up, kept as the *only*
  surviving piece of that extension. (Q3: extend to Codex/OpenCode, or just
  defer-finalize without resume.)
- **`meridian spawn done` is the instructed signal the nudge solicits.** It makes
  the common case fast/clean ("reap now; I've waited on what I needed"). Never on
  the correctness path — the deadline carries that.
- **The optional fast-finalize uses the tracker.** "turn-ended ∧ no live children
  ∧ no running bash" is fully observed (spawn store + Pi bash records, no model
  help). When it holds, reap immediately rather than wait for a nudge response. A
  latency optimization, not a correctness requirement.

### The idle-nudge loop (how the instructed done is actually solicited)

The model is not nudged once in the system prompt and forgotten. meridian
actively, persistently asks the idle model to declare done — a non-blocking loop
keyed off natural idle:

```
on FIRST natural idle/complete (turn ended on its own — NOT because meridian
   blocked it with a wait):
     send a NON-BLOCKING follow-up turn to the model:
        "If you're done, write your report as your final message, then run
         `meridian spawn done`."
every cadence interval (default ~5 min, cache-TTL-aligned) while still idle:
     re-send the same message
model runs `meridian spawn done`   → capture report + reap the whole tracked tree
deadline expired (hard cap)        → reap regardless                  [backstop]
```

- **Non-blocking by construction.** The nudge is a follow-up turn (the model may
  act or ignore it), *not* a blocking `meridian spawn wait`. It never wedges the
  session.
- **One cadence replaces two mechanisms.** This idle-keyed nudge replaces both the
  55-min bash-gated managed-bash ping *and* the quiescence-idle inference. No
  bash-presence gating, no notification-timestamp gate.
- **Cadence is cache-TTL-aligned.** Default ~5 min (configurable in `meridian.toml`),
  chosen to land within the Anthropic prompt-cache 5-min TTL so each re-nudge reuses
  the model's cached context instead of paying a full uncached context read. Pick
  *at or just under* the TTL (e.g. 270s) for margin — a 10-min gap would blow the
  cache and make every nudge expensive.
- **Harness-agnostic delivery.** The nudge is "start a new turn with this message,"
  which every resident backend supports — Pi `sendMessage(triggerTurn:true)`,
  Codex app-server start-turn, OpenCode session POST. **Consequence:** Codex/
  OpenCode backends must stay **resident** after `turn/completed`/`session.idle`
  to receive the nudge (and the resume-wake) — today they are stopped at that
  event. This is the behavior change that fixes the parked-on-`--bg` reap.
- **Cooperation-soliciting, not cooperation-dependent.** The nudge raises the odds
  of a clean, fast, well-reported done; the deadline guarantees termination when
  the model ignores every nudge. Correctness still lives in the deadline (and,
  optionally, the auto-finalize below).

> **Optional fast-finalize:** when the outstanding-work tracker can *prove* the
> parent has no live children and no running bash and the turn has ended, meridian
> may finalize immediately instead of waiting for a nudge response — a latency
> optimization, not a correctness requirement. Decide per Q5.

### Resolving the Codex/OpenCode ambiguity without asking the model

`turn/completed` is ambiguous: "fully done" vs "stopped, parked on a `--bg`."
Today's `SingleTurnDrainPolicy` resolves it as "done" and reaps — the bug. The
new rule resolves it by **looking, not asking**: turn ended + live children →
parked (don't reap); turn ended + nothing outstanding → done. No model
cooperation, harness-agnostic.

## 3. The outstanding-work tracker (the real deliverable)

This is the load-bearing component, and most of it already exists:

- **Child spawns — already harness-agnostic.** `SpawnRecord.parent_id` is set at
  creation from the spawning context (`execute_init.py:199`), independent of
  harness. `spawn_children` / `_collect_descendants` (`api.py:595-661`) answer
  "live descendants of this parent?" today. **No Pi disk-watcher needed** for
  child spawns; the core store is authoritative. (This makes the Pi disk-watcher's
  `spawns/<id>/state.json` correlation redundant — see ripout doc.)
- **Background bash — per-harness and best-effort (NOT load-bearing).** This varies
  more than the matrix first suggested:
  - **Pi:** meridian-managed background bash (`bash-records.json`). Observable.
  - **Codex:** **codex-native, first-class** background terminals (thread-scoped;
    `/ps`, `/stop`, `thread/backgroundTerminals/clean`). They **outlive
    `turn/completed`**. Codex *emits* raw events for them (`item/commandExecution/*`,
    `process/*`) so meridian **can** observe bg state — but **today the normalizer
    drops them and cleanup only kills the app-server root scope** (Q8 verified). Codex
    does **not** auto-follow-up after draining.
  - **OpenCode:** **no** background-job primitive at all (Q7 verified). Outstanding
    work for OpenCode is therefore *only* child spawns.
  Because bg-bash visibility is uneven, it is **not** on the correctness path: the
  deadline + nudge carry completion, and the model knows its own bg state when
  nudged. Bg-bash visibility only feeds the *optional* fast-finalize (§2), which
  stays conservative where bg work is unobservable.

The correctness-bearing predicate is just `turn_ended AND not
has_live_children(spawn_store)` — harness-agnostic, off the core store. The
`no running tracked bash` term is a best-effort *fast-finalize guard*, applied only
where the harness exposes bg-bash state (Pi; Codex if observable).

### No harness auto-follows-up — so the nudge is load-bearing

Confirmed for Codex (drains bg bash, never starts a new turn on its own); unknown
for OpenCode; only Pi auto-resumes (via our extension). So the idle-nudge / resume
`inject_turn` is **the only thing that wakes a model whose background work finished**
— it is not merely an "are you done?" poke, it is the continuation mechanism. This
strengthens the case for **resume** (§4.2, Q3): a Codex parent that fired bg work
and ended its turn is *stuck* until meridian injects a turn.

### OpenCode timeouts (Q7 verified) — and what they are NOT

The "5 min" is OpenCode's **provider-request timeout** (`provider.*.options.timeout`
= 300000ms, configurable, can be disabled) on the in-flight LLM call — **not** a
session kill and **not** a session idle-expiry. Tool timeouts are separate and
shorter: **bash ~2 min** (`OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS`), webfetch
30–120s, websearch 25s. Implications: (a) inline `meridian spawn wait` inside an
OpenCode turn is unreliable — a long blocking tool call hits the ~2-min bash cap;
another reason not to rely on the model blocking; (b) **correction:** the 5-min cap
does NOT bound session idleness, so 270s nudging is justified by the **prompt-cache
TTL only**, not by an OpenCode keepalive — no session idle-expiry is documented
(unconfirmed; live-repro item, §7 Q7). OpenCode auto-continues *within* a turn after
tool calls, but still goes `session.idle` when the model stops, so cross-turn resume
via `inject_turn` remains meridian's job.

## 4. The optional accelerator, made sandbox-proof

If/when we build `meridian spawn done`, it must not depend on the model's command
actually writing `~/.meridian` (the old `meridian report <file>` lesson — sandbox
blocked it). For Pi, the managed-bash extension runs *inside* the Pi runtime
(meridian's own TS) and already sees every bash command; it intercepts a match,
writes a `done` marker atomically, emits a stream event, and short-circuits — **no
sandbox boundary crossed**, because the extension is meridian, not the model. For
Codex/OpenCode the *attempt* is visible in the tool-call stream even if the
sandbox blocks execution (matrix dim 4). Either way the **runner** does the
reaping. But again: this is the accelerator, not the authority.

## 4.1 Report capture (instruct the model; observe as the floor)

Done and report are paired: a finalized spawn must carry a report, and the same
instruct/observe split applies.

**Today (from the matrix):** capture is observation-based already —
`extract_or_fallback_report()` prefers a `report.md` artifact, then the harness
extractor, then the **last assistant message** from the observed history
(`launch/report.py:309-364`). The spawn prompt already instructs "your final
assistant message is the report" (`launch/prompt.py:6-15`). There is no
`meridian report`-style write command on the correctness path (and shouldn't be —
that's the command the sandbox used to block).

**Design:** keep both halves, make the pairing explicit.

- **Instruct (happy path):** the prompt tells the model to (a) write its report as
  its final assistant message, and (b) call `meridian spawn done`. The done-nudge
  text and the report instruction are the *same* contract: "when finished, state
  your report, then run `meridian spawn done`."
- **Observe (floor):** regardless of compliance, at finalization meridian extracts
  the report from the observed stream (last assistant message), exactly as today.
  A model that wanders off or never calls `done` still yields a report from its
  last assistant turn; the deadline-driven reap finalizes with that extracted
  report.
- **No sandbox dependency.** Report capture reads the stream meridian already
  persists (`history.jsonl`), written by the unsandboxed runner. The model writing
  a `report.md` is an *optional* enhancement (taken when present), never required —
  same status as `meridian spawn done`.

**This requires changing extraction — it can no longer be "last assistant
message."** The new model *injects turns into the stream* (the periodic nudges, the
resume-wake notifications) and the model *replies* to them ("not done yet", "ok
reaping"). Naïvely taking the last assistant message would capture a nudge-reaction
or a child-completion acknowledgement instead of the report. Extraction becomes
**protocol-aware**:

- **Anchor on `done`.** When `meridian spawn done` is observed, the report is the
  model's substantive content **at/before** that declaration (the nudge told it to
  "write your report, *then* run done"), preferring an explicit `report.md` when
  present. After `done` there is no further content.
- **Filter injected control chatter.** Exclude meridian-injected nudge follow-ups
  and resume-wake notifications **and the model's replies to them** from report
  candidacy — the same class as today's `is_control_report_payload` /
  `_is_pi_lifecycle_noise_payload` filtering (`launch/report.py`). The nudge
  follow-up carries a known `customType`, so it (and the turn it triggers) is
  identifiable and skippable.
- **Deadline fallback.** When no `done` is observed (model ignored every nudge),
  anchor on the last *substantive* assistant message with the control chatter
  filtered out — never a nudge reply.
- **Multi-turn safety.** With resume-wake the session has many turns; extraction
  must pick the last substantive report content, not the literal last line.

**Invariant:** every finalized spawn has a report, captured from observation,
anchored on the `done` declaration when present and on the last substantive
(non-control) assistant message otherwise. Model cooperation improves report
quality; it is never required for a report to exist.

## 4.2 Feasibility — verified against the raw runtimes (investigators p3864/5/6)

The resident-nudge-resume model was validated against the actual Codex/OpenCode/Pi
runtimes, not just meridian's adapters:

- **Codex — YES (protocol).** `codex app-server` is a persistent websocket server.
  After `turn/completed` a client can reuse the same `threadId` and send an
  **unsolicited `turn/start`** (NOT `turn/steer` — that requires an active turn).
  Conversation persists server-side. Meridian owns the process lifecycle
  (`ManagedBackend.launch` → `CodexConnection.stop`).
- **OpenCode — YES (protocol).** `opencode serve` is a persistent HTTP server.
  After `session.idle` a client can `POST /session/{id}/prompt_async` (or
  `/message`) an **unsolicited** prompt to the same session; session state persists
  (get/list/restore exist). Meridian owns the process lifecycle.
- **Pi — YES (already).** Resume works today via the **in-process** extension
  calling Pi-native `sendMessage(triggerTurn:true)` → `_runAgentPrompt`. The
  watch/quiescence/correlation logic is meridian-built (portable); the in-process
  injection primitive is Pi-native.

**"We don't own them" is not a blocker.** We own the server *process* (start +
kill), and the *protocol* permits unsolicited 2nd/Nth turns on the same session.
The only per-harness difference is *where* the re-prompt originates: Pi reaches
**inside** (extension); Codex/OpenCode are driven from **outside** over their own
protocol (which is *less* code — no extension to ship).

### The `inject_turn` seam (one method, three implementations)

Both the nudge and the resume reduce to "start a new turn on this resident session
with this message." That is a single per-connection capability:

```
connection.inject_turn(message)   # nudge text, or child-completion result
  Pi        → pi.sendMessage(message, { triggerTurn: true, deliverAs: "followUp" })
  Codex     → turn/start on the existing threadId (unsolicited)
  OpenCode  → POST /session/{id}/prompt_async  (unsolicited)
```

Child-completion detection is already harness-agnostic (the core spawn store), so
**defer-finalize and resume share almost all machinery** — resume just adds "store
says child done → `inject_turn(result)`." Both halves already exist; resume is a
small increment over defer, not a separate build. → favors **resume (Pi-parity)
across all harnesses** (resolves Q3).

### Two meridian-side blockers this introduces (must change)

1. **Stop-at-completion.** Today `turn/completed`/`session.idle` is terminal →
   `connection.stop()` kills the backend (`spawn_manager.py:900-905`,
   `semantics.py:122-162`). The resident model must NOT stop here; it stops on
   done/deadline.
2. **120s liveness timeout vs the ~5-min nudge.** Both Codex (`codex_ws.py:174`,
   `:681-705`) and OpenCode (`opencode_http.py:159`, `:378-426`) fail a connection
   after 120s of stream silence. A cache-friendly 5-min nudge cadence would be
   killed at 2 min. **The resident "awaiting-done" state MUST suppress the
   stream-stall kill** — only the deadline reaps. This extends the thermo-nuclear
   `BackendLivenessPolicy` (which already has `SUPPRESS`): add "intentionally idle /
   awaiting-done" as a suppression reason, distinct from turn-in-flight suppression.
   Without this, the whole resident model dies at 120s.

## 5. What changes — folded with the thermo-nuclear rework

This design supersedes two `design.md` decisions (made before the simplification
thesis and the thermo-nuclear review):

| `design.md` said | This design says | Why |
|---|---|---|
| `ManagedBackend` deep module with phase/deadline state machine + `persist_phase()` | **Flatten** to `launch_managed_backend(...) -> Handle`; drop the phase state machine | Thermo-nuclear: depth is fake; only `launch()` is used |
| `backend_lifecycle.json` separate sidecar (Q2 → supplement) | **Delete it.** Fold the one needed datum (containment outcome) into `ProcessScopeSnapshot` | Written-once-never-read second source of truth; drifts vs `process_scopes.json` |
| (didn't cover completion) | **Done = (turn ended ∧ no outstanding tracked work)**, deadline backstop, optional `done` accelerator; rip out quiescence inference + ping | This document |

The pre-running **deadline** insight from `design.md` (takeover-wedge fix,
p1532/p3830) is **kept** as a plain timer, not a persisted phase enum. The liveness
ordering bug is fixed: `evaluate()` checks `BACKEND_DEAD` **before** turn/request
`SUPPRESS` (`liveness.py:72-113`).

### Net effect on the completion path

- Codex/OpenCode: the real behavioral delta. Today they stop the backend at
  `turn/completed`/`session.idle`. Now they **stay resident** on natural idle to
  receive the idle-nudge loop (and the resume-wake), finalizing on observed `done`,
  deadline, or the optional fast-finalize. This is what fixes the parked-on-`--bg`
  reap, and it reuses the harness-agnostic nudge/resume delivery (start-a-turn).
- Pi: `PiRpcQuiescenceDrainPolicy` → the same harness-agnostic completion loop. The
  quiescence-idle inference, the 55-min bash-gated ping, the notification gate, and
  micro-drain are deleted; the `sendMessage(triggerTurn)` delivery primitive and the
  resume-wake stay (now driven by the Python idle loop).
- Claude: headless denied by default — unchanged. Primary is human-driven — out of
  scope.

## 6. Invariants

1. **Correctness requires zero model cooperation.** The system finalizes correctly
   if the model never calls `wait` or `done`.
2. **The model never needs `~/.meridian` write access to finish.** All state writes
   and process kills are the runner's (or reaper's) job.
3. **A terminal turn event is a turn boundary, not "done."** Done additionally
   requires no outstanding tracked work.
4. **One completion authority per spawn:** the drain policy, observing turn-end +
   outstanding-work. The reaper is the crash-time backstop, not a parallel path.
5. **Reaping terminates the whole tracked tree** (process + scope + subspawns),
   regardless of how done fired.
6. **Outstanding-work for child spawns reads the core spawn store** (harness-
   agnostic), not a per-harness disk watcher.
7. **Every finalized spawn has a report**, captured from the observed stream; the
   model's explicit final message / `report.md` is preferred when present but never
   required.
8. **Instruction is layered on observation, never a substitute.** We instruct the
   model to call `done` and write its report; meridian finalizes and captures
   correctly without either.
9. **Resident-awaiting-done suppresses the stream-stall kill.** A backend held idle
   for nudges is terminated only by `BACKEND_DEAD` (process gone) or the completion
   deadline — never by the 120s silence timer. Otherwise the resident model can't
   survive one nudge interval.
10. **Re-prompting is one seam:** `connection.inject_turn(message)` — Pi
    `sendMessage`, Codex `turn/start`, OpenCode `prompt_async`. Both the nudge and
    the resume use it; meridian drives it from outside the harness (Pi: from its
    extension), never depending on owning harness code.

## 7. Open questions for human decision

- **Q1 — Deadline default.** One harness-agnostic `spawn_done_deadline_seconds`
  (generous, e.g. the wait timeout)? Delete `pi_task_ping_interval` and
  `pi_notification_timeout`; repurpose/rename `pi_child_wave_timeout`.
- **Q2 — Background bash gap for Codex/OpenCode.** Accept that their untracked `&`
  jobs are invisible (document the limit), or is there appetite to track bg shell
  jobs harness-agnostically later? *Recommendation:* accept + document; child
  spawns (the common case) are already covered by the core store.
- **Q3 — Resume-on-completion for Codex/OpenCode. → RESOLVED (recommend resume).**
  Verified feasible (§4.2): both protocols support unsolicited 2nd turns; meridian
  owns the process. Resume is a *small* increment over defer-finalize (both need
  resident + idle-kill-suppression + `inject_turn` + done/deadline; resume just
  reuses the core-store child-completion signal). *Recommendation:* build resume
  (Pi-parity) via the `inject_turn` seam. Confirm.
- **Q4 — `meridian spawn done` in v1.** Build it *and* instruct it via the nudge
  loop, but land the deadline floor first so it's never on the correctness path.
  *Recommendation:* deadline floor + idle-nudge loop together; pair the report/done
  prompt contract (§4.1) in the same change.
- **Q5 — keep the optional fast-finalize?** When the tracker proves nothing is
  outstanding, reap immediately instead of waiting for a nudge response. Pro: lower
  latency, no needless wait-for-nudge on trivially-done spawns. Con: a second
  completion path to reason about. *Recommendation:* include it — it's cheap (reads
  the core store) and makes the common "no children" case instant; the nudge loop
  is then only for spawns that actually parked on work.
- **Q7 — OpenCode timeouts → RESOLVED (web research p3867).** The 5 min = provider
  request timeout (configurable), not a session/idle kill; bash tool ~2 min; no
  background-job primitive (outstanding work = child spawns only); auto-continues
  *within* a turn but goes idle cross-turn (resume still ours). **Residual live-repro
  item:** is there any `opencode serve` session idle-expiry? (None documented; if it
  exists, the nudge cadence must stay under it. Verify with a live idle test before
  relying on long idle holds.)
- **Q8 — Codex native bg bash → RESOLVED (investigator p3868).** First-class
  thread-scoped background terminals that outlive `turn/completed`; codex emits
  `item/commandExecution/*`/`process/*` events (observable) but meridian drops them
  today and only kills the root scope; no auto-follow-up. **Two implications:**
  (a) to let the fast-finalize guard / reaper see Codex bg work, meridian must
  normalize those events and ensure teardown terminates bg terminals (root-scope
  kill *probably* cascades via process tree, but confirm — codex also has a protocol
  `thread/backgroundTerminals/clean`). (b) **Residual live-repro item:** does an
  unsolicited `turn/start` after `turn/completed` surface drained bg results to the
  model? (Plausible, unproven — verify live before promising Codex bg-resume.)
- **Q6 — nudge cadence + deadline values.** Nudge cadence default **~5 min,
  cache-TTL-aligned** (at/just-under the 5-min prompt-cache TTL — e.g. 270s — so
  re-nudges reuse cached context; a 10-min gap blows the cache and makes every
  nudge a full uncached read). Deadline hard-cap default generous (e.g.
  `wait_timeout`). Both configurable in `meridian.toml`; delete
  `pi_task_ping_interval` / `pi_notification_timeout`. *Open:* exact default —
  300s (clean 5 min, but exactly at the TTL boundary) vs 270s (safe margin)?
