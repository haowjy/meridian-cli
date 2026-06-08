# Rip-out & Refactor Inventory — Pi completion machinery → observed-done model

> Concrete companion to `design-spawn-done-model.md`. The completion authority is
> **meridian-observed**: `(turn ended) AND (no outstanding tracked work)`, with a
> **deadline** backstop and an **instructed** (not relied-upon) `meridian spawn
> done` accelerator. What deletes is the *inference* layered on top (ping,
> quiescence-idle gate, notification timestamp, micro-drain). What **stays** is the
> *tracking* of outstanding work and the *resume-on-completion* wake — the only
> model-cooperation-free signals — plus the reaping executors.
>
> Verdicts: **DELETE** (gone), **SIMPLIFY** (shrinks to a minimal core), **KEEP**
> (load-bearing: tracking / resume / reaping).
>
> Evidence from explorers p3861 (Pi + extensions) and p3862 (Codex/OpenCode).
> Line numbers are at branch `fix/opencode-orphan-cleanup`; treat as anchors.
>
> **Big picture:** child-spawn outstanding-work is already harness-agnostic in the
> core spawn store (`parent_id` + `spawn_children`/`_collect_descendants`). So Pi's
> disk-watcher child correlation is **redundant**, and the new completion predicate
> serves Codex/OpenCode too — not just Pi.

## A. Python — streaming/completion layer

| Module / symbol | Today | Verdict | Notes |
|---|---|---|---|
| `streaming/pi_quiescence.py` — `PiQuiescenceTracker` (`:17-95`) | Infers done from parent-idle + disk-backed pending-child/bash + last-notification timestamp | **DELETE** | The "does it *look* done?" idle/notification inference is replaced by the observed predicate `(turn ended) ∧ (no outstanding tracked work)` + deadline. |
| `streaming/pi_drain.py` — `PiDrainCoordinator` quiescence / child-wave / notification / micro-drain (`:50-119`, `:147-185`, `:246-394`, `:454-493`) | Multi-branch wait orchestration | **SIMPLIFY → completion policy** | Collapse to: resolve when `turn-ended ∧ no live children (core store) ∧ no running tracked bash`, OR deadline, OR observed `done`. Delete notification/ping/child-wave/micro-drain branches. The "wait for children" is now a store query, not a disk-gated wave. |
| `streaming/drain_policy.py` — `PiRpcQuiescenceDrainPolicy` (`:43-54`) | Emits turn boundary, continues until quiescent | **REPLACE** | New policy resolves on the observed completion predicate. Selection seam unchanged (`spawn_drain_loop.py:108-123`); ideally the predicate is harness-agnostic so Codex/OpenCode share it. |
| `streaming/disk_watcher.py` — `PiDiskWatcher` (`:35-147`, `:224-260`) | Reads `spawns/<child>/state.json`, `bash-records.json`, `last-notification.json` to gate quiescence | **SIMPLIFY HARD** | **Child-spawn correlation is redundant** with the core store (`spawn_children`) — delete the `spawns/<id>/state.json` reads. Delete `last-notification.json` (`:255-260`). Keep only `bash-records.json` reading for the Pi-only "running tracked bash?" predicate. |
| `streaming/spawn_manager.py` — `_cleanup_completed_session` (`:852-907`) | Stops connection, cleans scope | **KEEP** | Teardown executor; completion triggers it. Single-turn already routes here. |
| **Outstanding-work predicate** (NEW, harness-agnostic) — reuse `spawn_children`/`_collect_descendants` (`ops/spawn/api.py:595-661`) + `parent_id` | Answers "does this parent have non-terminal children?" | **ADD (mostly reuse)** | The load-bearing tracker. Reads the core store; serves Codex/OpenCode + Pi. Pi adds the bash predicate on top. |
| `streaming/pi_subspawn_tracker.py` — `PiSubspawnTracker` (`:49-359`) | Tracks child `pid`/`pgid` from events | **KEEP (reaping)** | Needed so reap kills the whole tracked tree. Enumeration source for teardown, not a completion gate. (Could later fold into the core store if it carries pid/pgid.) |
| `streaming/pi_process_cleanup.py` — `terminate_pi_tracked_subspawns()` (`:16-42`) | Kills tracked subspawns | **KEEP (reaping)** | Invariant #5: declaring done does not make children vanish; the runner makes them vanish. |
| **Report extraction** — `extract_or_fallback_report()` (`launch/report.py:309-364`), `_extract_last_assistant_message` (`:250-279`), prompt contract (`launch/prompt.py:6-15`) | Prefers `report.md`, else extractor, else **last assistant message** | **MODIFY (protocol-aware)** | Stays observation-based, but "last assistant message" is now wrong: the nudge/resume turns inject assistant replies. Anchor on the `done` declaration; filter the injected nudge follow-ups + resume notifications + their replies (extend the existing `is_control_report_payload`/`_is_pi_lifecycle_noise_payload` filtering, keyed on the nudge `customType`); deadline-fallback to last *substantive* message. Pair the prompt's report instruction with the `done` instruction (design §4.1). Still no `meridian report` write command on the correctness path. |

## B. Python — config / resolve

| Symbol | Today | Verdict |
|---|---|---|
| `launch/resolve.py` — `resolve_pi_task_ping_interval_seconds` (`:376-389`) + `_resolve_..._from_config_snapshot` (`:347-360`) | Resolves 55-min ping interval | **DELETE** (ping is gone) |
| `launch/resolve.py` — `resolve_pi_notification_timeout_seconds` (`:290-313`) | Resolves continuation-notification timeout | **DELETE** (notification machinery gone) |
| `launch/resolve.py` — `resolve_pi_child_wave_timeout_seconds` (`:392-411`, default 300s) | Child-wave timeout | **SIMPLIFY → `resolve_pi_done_deadline_seconds`** | This becomes the single done-deadline backstop. |
| `config/settings.py` — `pi_task_ping_interval_seconds` (`:1340-1351`), `pi_child_wave_timeout_seconds` (`:1328-1339`) | Config fields + env + file aliases | **DELETE ping field; RENAME child-wave → `pi_done_deadline_seconds`** | Resolve Q1 in the design first. |
| `harness/connections/pi_rpc.py` — ping env injection (`:551-568`) | Sets `MERIDIAN_PI_TASK_PING_INTERVAL_MS` from config | **DELETE** |

## C. Pi runtime extensions (`src/meridian/pi_runtime/extensions/`)

> **Do not edit generated target dirs.** These are source TS extensions, edited
> in the package repo, not under `.pi/`. (AGENTS.md.)

| Extension / file | Role | Sends model messages? | Verdict |
|---|---|---|---|
| `managed-bash/src/bash_runtime.ts` — ping: `schedulePing`/`firePing`/`sendBackgroundPing`, `ping_sent_at_ms`, `MERIDIAN_PI_TASK_PING_INTERVAL_MS` (`:94-100`, `:376-393`, `:454-487`, `:551-557`) | 55-min one-shot bash-gated "task still running" nudge | **Yes** | **DELETE.** Replaced by the harness-agnostic Python-side idle-nudge loop (below). Keep background-bash tracking + `bash-records.json` (still real state for the bg-bash predicate + `bash_manage`). |
| `managed-bash/src/index.ts` — `sendMessage(... deliverAs:"followUp")` background ping text (`:78-87`) | Delivers the ping turn | **Yes** | **DELETE the bash-ping text/wiring.** The `sendMessage(triggerTurn)` *primitive* is kept (Pi's delivery mechanism for the idle-nudge + resume-wake), but driven by the Python idle loop, not bash presence. |
| **Idle-nudge loop** (NEW, Python-side — `streaming/` drain/spawn-manager) | On natural idle, send a non-blocking follow-up turn ("if done, write report, run `meridian spawn done`") now + every cadence interval (default ~5 min, cache-TTL-aligned, `meridian.toml`) | — | **ADD (harness-agnostic).** Idle detection is observer-side; delivery is "inject a turn" per connection — Pi `sendMessage(triggerTurn)`, Codex app-server start-turn, OpenCode session POST. Replaces the 55-min ping *and* the quiescence-idle inference with one cadence. Cadence ≤ prompt-cache TTL so re-nudges reuse cached context. |
| `managed-bash/src/bash_runtime.ts` — **NEW: `meridian spawn done` interception** | — | — | **ADD (accelerator).** Match the command in the bash entrypoint; write atomic `done` marker + emit stream event; short-circuit execution (design §4, sandbox-proof). Instructed but off the correctness path. |
| `meridian-spawn-watch/src/index.ts` — completion follow-up `sendMessage(... triggerTurn:true)` (`:452-467`) | Wakes/re-prompts the parent when a child spawn completes | **Yes** | **KEEP (resume) — but it's the ONLY surviving piece.** This is the model-cooperation-free way a parent reacts to child results (models don't reliably `wait`). Strip everything else from the extension. |
| `meridian-spawn-watch` — `last-notification.json` marker + suppression gating (`:463-467`, `:442-448`, `:500-527`) | Quiescence gate / dedup marker | **DELETE** | The marker existed to gate quiescence inference, which is gone. Keep at most an in-memory `TERMINAL_NOTIFIED` dedup set; drop the disk marker. |
| `meridian-spawn-watch` — child correlation via `spawns/<id>/state.json` watch | Discover completed children | **SIMPLIFY** | The completion *predicate* now reads the core store, not this watcher. The watcher's only job shrinks to "a tracked child reached terminal → fire one resume follow-up." Consider driving the wake from the runner side (it already sees child terminal state) and deleting the in-Pi watcher entirely. |
| `meridian-spawn-watch` `/spawn*` UI commands, panels | TUI surface | No | **DELETE if watcher goes**, else keep. Optional UX, not completion. |
| `managed-bash` `bash`/`bash_manage`, `/ps*`, `MERIDIAN_PI_BASH_ID` injection | Core background-bash tooling | No | **KEEP.** Orthogonal to completion; this is how the agent runs/waits on its own work before declaring done. |
| `shared/json_file.ts`, `shared/pi_state_paths.ts`, `shared/schemas.ts`, `shared/spawn_origins.ts` | Atomic IO + path/record contracts | No | **KEEP** (prune `last-notification.json` path + `ping_sent_at_ms` field). |
| `shared/meridian_cli.ts`, `shared/meridian_spawn.ts`, `shared/ids.ts` | `/spawn` helpers + spawn-id parsing | No | **KEEP iff** spawn-watch/`/spawn` commands stay; else delete. |
| `shared/selectable_panel.ts`, `log_overlay.ts`, `ui.ts` | TUI panels for `/ps`/`/spawn` | No | **DELETE if ripping UI commands**, else keep. |
| `shared/pi_harness_profile.ts` | TS mirror of profile toggles | No | **DELETE/verify** — explorer found no import from extension entrypoints; Python projection owns bundle loading. Confirm no dist consumer before deleting. |

## D. Thermo-nuclear rework, folded in (from `/tmp/rework-plan.md`)

These were already decided pre-simplification; they remain and now serve the
done model. Sequenced correctness-first.

1. **`liveness.py:72-113` — `evaluate()` order fix.** Check `BACKEND_DEAD` **before**
   turn/request `SUPPRESS`. A stale active-turn flag must never mask a dead
   backend. [HIGH correctness — do first]
1b. **`liveness.py` — add "awaiting-done" suppression (REQUIRED by the resident
   model).** The 120s stream-stall kill (`codex_ws.py:174`/`:681-705`,
   `opencode_http.py:159`/`:378-426`) would kill a backend held idle for ~5-min
   nudges. When a spawn is resident-awaiting-done, `evaluate()` must NOT return
   `STREAM_STALLED`/kill on silence — only `BACKEND_DEAD` (process actually gone)
   and the completion deadline may terminate it. Distinct suppression reason from
   turn-in-flight. **Without this the whole resident-nudge model dies at 120s.**
2. **`detached_process.py` — honest containment outcome.** Return typed
   `mode/job_handle/degraded_reason`; never record `windows_job` when job-assign
   failed; record `pid_tree_fallback` honestly. Fold the outcome into
   `ProcessScopeSnapshot` (this also kills the need for `backend_lifecycle.json`).
3. **`backend_lifecycle.py` + `backend_lifecycle.json` — DELETE.** Written-once,
   never-read second source of truth. The one needed datum lives in the scope
   snapshot (item 2).
4. **`managed_backend.py` — FLATTEN.** Replace the phase/deadline state machine +
   `persist_phase()` with `launch_managed_backend(...) -> Handle`. Only `launch()`
   is used. Keep the pre-running deadline as a plain timer on connect/observe (the
   p1532/p3830 takeover-wedge fix), not a persisted enum.
5. **`reaper.py:528-633` — restore root-only side-effect boundary.** Ordinary
   nested reads (list/stats/wait) must not mutate/kill; route dead-runner recovery
   through one cleanup owner.
6. **`pre_init.py:61-71` — typed failures**, not blanket `except Exception`.
7. **Phase 3 (last, on settled code):** split `codex_ws.py` (1370) and
   `opencode_http.py` (1170) by concern; collapse the ~7 `owner_chat_id`
   touchpoints behind one accessor.

## E. Sequencing

```
0. KEEPER CLEANUP (branch-health audit p3869/p3870 — do BEFORE the feature; the
   resident/done model adds more session-routing onto this exact surface):
   a. Finish the owner_chat_id/chat_id migration behind ONE canonical accessor.
      Fix the real bugs: Pi child-session ref resolution (`reference.py:247-249,
      285-287`), `session export --include-spawns` dropping child reports
      (`session_export.py:214,231-233,279-283`), `spawn stats --session` filtering
      by owner not exact session (`api.py:706-708` vs `spawn.py:792`), legacy
      chat_id diagnostics (`cli/utils.py:114-120`). (This is the old D7 owner_chat
      item — PROMOTED from Phase 3; the audit found it's buggy, not just scattered.)
   b. Clarify `detached_process.py` parent-death contract: Linux-only no-op without
      `prctl` (`:24-36,57-66`) is a silent cross-platform orphan hole — narrow the
      contract or add a non-Linux fallback + capability test.
   c. Add `session_store` spawn↔session API (`get_session_record_for_spawn` /
      `get_owner_chat_for_session`); inline the hand-rolled lookup
      (`session_target.py:490-503`).
   d. Tighten `pre_init.py` (overlaps D6): log unexpected exceptions w/ traceback,
      narrow caught types; reserve `pre_init_failed` for expected validation.
1. Correctness first (D1–D3, D5 — D6 folded into 0d) — no behavior change.
2. RESIDENT FLOOR: keep the backend resident past the turn event + deadline reap.
   Requires three coupled changes: (a) stop treating `turn/completed`/`session.idle`
   as terminal → don't `connection.stop()` at the turn event (`spawn_manager.py:900-905`,
   `semantics.py`); (b) the awaiting-done liveness suppression (D1b) so the 120s kill
   doesn't fire; (c) the deadline backstop. This alone fixes the parked-on-`--bg`
   reap and must finalize correctly with zero model cooperation. Add the per-connection
   `inject_turn(message)` seam (Pi `sendMessage` / Codex `turn/start` / OpenCode
   `prompt_async`) — used by both the nudge and resume.
3. INSTRUCTED ACCELERATORS + PROTOCOL-AWARE EXTRACTION (layered on the green
   floor): wire the `meridian spawn done` interception (C), the paired report/done
   prompt contract (design §4.1), and the **protocol-aware report extraction** —
   anchor on `done`, filter injected nudge/resume turns + replies. This MUST land
   with the nudge loop (step 2): the moment we inject turns, "last assistant
   message" extraction breaks. Never on the correctness path otherwise.
3b. RESUME (Pi-parity, recommended — Q3): on core-store child-completion, call
   `inject_turn(child_result)` to wake the parent. Reuses the seam from step 2 and
   the child-detection from the outstanding-work predicate — small increment. For Pi
   this replaces the in-extension spawn-watch wake with the same harness-agnostic
   path (or keeps the extension wake; implementer's call).
4. Rip out the inferred-done machinery (A/B/C deletes) once the floor is green.
5. Flatten ManagedBackend + delete backend_lifecycle (D2, D4).
6. File decomposition + owner_chat accessor (D7), on settled code.
7. Re-run both thermo-nuclear reviewers in fresh contexts.
```

Verify after each step: `ruff`, `pyright` (0 errors), targeted Pi drain tests +
the now-green opencode liveness hang test, and two real spawns — (a) a model that
**never** calls `done`/`wait` but fires a `--bg` child, confirming the parent is
NOT reaped early and finalizes (with a report) once the child drains; (b) a model
that **does** call `meridian spawn done`, confirming the whole tracked tree is
reaped immediately.

## F. What we are explicitly NOT touching

- **Terminal-event classification** (`semantics.py` — `turn/completed` /
  `session.idle` / `result`). It still detects the *turn boundary* correctly; we
  only stop treating that boundary as unconditional "done." (`SingleTurnDrainPolicy`
  itself **does** change — it gains the outstanding-work check — so it is *not* on
  this list.)
- **The reaping executors** (`process_cleanup.py`, `pi_process_cleanup.py`,
  scope termination). Completion *triggers* them; it does not replace them.
- **Report extraction** (`extract_or_fallback_report`) — already observation-based;
  we only pair its prompt instruction with the `done` instruction.
- **Claude primary.** Human-driven, not a subagent, out of scope.
