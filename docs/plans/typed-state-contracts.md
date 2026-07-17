# Typed state contracts — thermo-nuclear audit workstream (priority: high)

Findings: cluster C ([68] [69] [71] [72] [75] [74] [36]) + cluster D ([73] [15]).
All claims verified by adversarial refute pass + independent sol review; remedies below
are the corrected versions. Part of the thermo-nuclear audit (#389).

## Why

The persisted state layer is stringly-typed at every boundary, so whole bug classes exist
only because the types permit them:

- **Vocabulary drift**: SpawnStatus lives in a Literal (`core/domain.py:14-16`) AND str
  frozensets (`core/spawn_lifecycle.py:52-66`, with a TODO admitting it). A persisted
  out-of-vocab status is neither active nor terminal — an unreapable zombie
  (`reaper.py:715` selects active; `repository.py:277` guards terminal). `list_spawns()`
  silently omits invalid rows (`spawn_store.py:901-905`) while `get_spawn()` raises
  (`:915-925`) — two answers for one disk state.
- **Shape drift**: SpawnRecord's ~42 Optionals permit partial terminal state the writers
  never produce, so readers ship invented answers: the reaper and `ops/spawn/query.py:84-99`
  guess exit codes 0/130/1; `spawn_service.py:1066` defaults to 1. `finalize_spawn` accepts
  nonterminal `status="finalizing"` and writes terminal facts onto a nonterminal row
  (probe-confirmed; `core/lifecycle.py:529-541` vs its own `TerminalStatus` at `:103`).
- **Field-list drift**: the spawn schema is hand-copied across parallel lists
  (`model.py:48-101`, `repository.py:27-86`, both converters `:105-220`,
  `start_spawn`'s 25 kwargs + 40-field ctor at `spawn_store.py:289-431`). A field missed
  in one converter direction stays pyright-green and silently resets to default on the
  next `write_state_locked` round-trip — silent persisted data loss.
- **Control-flow drift**: "mutator declined" is re-invented as four local exception
  classes raised through the locked mutator (`spawn_store.py:606,659,704,785`).
- **Event-semantics drift**: terminal/activity/signal classification is a central
  per-harness if-chain; `activity_transition` matches Codex and OpenCode names unscoped
  (`semantics.py:235-241`), violating the module's own documented invariant
  ("event_type is NOT globally unique"). Cross-harness misclassification reproduced by
  probe (Claude `tool_call` → turn_active).
- **Work-item ambiguity**: archived-ness is encoded in directory location AND
  `__status.json`, reconciled by a 117-line heuristic (`work_store.py:285-401`). Sol
  reproduced the crash ambiguity: interrupted archive and interrupted reopen leave
  byte-identical state; the heuristic misclassifies one as the other, and the error
  reaches policy (`work_lifecycle.py:391-405`). Reads rewrite state files (`:399-400`).

## Goal

State meaning decided by construction, not by heuristic order or reader fallbacks: typed
persisted models validated at the parse boundary, discriminated lifecycle facts,
declared per-harness event semantics, and one authority per fact. Invalid states become
unparseable or unconstructible; the defensive fallbacks that interpreted them get deleted.

## Summary

Member issues (drafts in `issues-typed-state.json`, sequenced):

1. **Typed persisted spawn model** ([68]+[72]) — single SpawnStatus authority with
   lifecycle sets derived from it; typed StoredSpawnState fields validated at parse with
   quarantine (not coercion) for out-of-vocab rows — `"unknown"` stays in the parse
   vocabulary (mark-running-from-unknown recovery depends on it); shared state-fields
   base so converters become one-liners; import-time field-accounting guard modeled on
   the harness layer's `_enforce_spawn_params_accounting`. Deletes the Literal duplicate,
   all four `cast()` sites, and the two mirror converters.
2. **Discriminated spawn lifecycle facts** ([69]) — frozen `RunnerExitFacts` /
   `TerminalFacts` sub-models; a genuinely discriminated lifecycle representation (status
   not duplicated inside and outside the facts); public finalize APIs accept only
   `TerminalSpawnStatus`. Deletes both exit-code-guessing fallbacks and the default-1
   fallback; partial terminal state becomes unparseable, including in persisted state.json.
3. **Apply/Decline locked-mutation outcome** ([71]) — mutators return
   `SpawnRecord | Decline(reason)`; repository returns a `MutationOutcome(wrote, snapshot,
   reason)` (lifecycle-level `transitioned` stays out of the persistence result;
   `FinalizeOutcome` adapts it). Deletes the four exception classes and the nonlocal
   smuggling; fixes the stale `terminal_policy.py:28-33` docstring (reject is already a
   true no-op — verified).
4. **Precise vocabularies and typed identities** ([75]) — `SpawnKind =
   Literal["child", "primary", "streaming"]` (streaming is real:
   `cli/streaming_serve.py:83-89`); typed `ChatId` and `HarnessSessionId` with a shared
   normalization validator at **each** persisted boundary (spawn state.json and session
   JSONL parse independently — there is no single boundary); promote the four
   Literal-in-a-comment fields. Makes `session_identity.py`'s per-read `.strip()` dead code.
5. **Per-bundle harness event semantics** ([74]+[36], one move) — `HarnessSemantics`
   port on each HarnessBundle, dispatching by `HarnessId` **before** `event_type`;
   declarative tables plus per-adapter resolvers for payload-dependent cases; reserved
   meridian namespace for synthetic connection events. Transport keeps an **open**
   `RawHarnessEvent` envelope (harness CLIs are unpinned; unknown events must stay
   persistable/ignorable — no strict rejection at raw parsing) normalized into a closed
   discriminated semantic union; strictness lives in contract tests. Deletes the central
   if-chains; cross-harness name collisions become structurally impossible; adding a
   harness no longer edits shared classifier files.
6. **Work-item store on one authority** ([73]+[15]) — typed `StoredWorkItemState` codec
   (deletes `_status_payload`, `_worktree_payload`, and the isinstance re-casting);
   directory location is the **sole** archived-ness authority (archived items present as
   done regardless of stale metadata; `archived_at` stays stored — it is real data — but
   never decides archived-ness); archive AND reopen become location-first transitions so
   crash states are unambiguous by construction; reads become pure with an explicit
   `repair()` seam; one locked mutation entry point collapses the five duplicated persist
   tails and fixes the `archived_at` model/payload mismatch (`:853/:865`, `:918/:930`).
   Status stays an **open** user-label vocabulary with `"done"` reserved and non-empty
   validated (a closed enum would delete `meridian work update --status blocked`).
   Legacy `_coerce_worktree_metadata` (101 lines) is deleted per the no-backcompat
   policy, or run as a one-shot migration — never lazy read behavior.

## Resulting Behavior

A state file either parses into a shape whose meaning is total, or is quarantined and
reported — never silently reinterpreted. The same disk state yields the same answer in
every process. Adding a spawn field, a status value, or a harness is a one-place change
guarded at import time; the drift is caught before it ships, not after it corrupts.

## Out of Scope

- `core.domain.Spawn` (5-field execution aggregate) and launch-layer `SpawnReservation`
  are not persistence projections — do not fold them into the state layer.
- The `StoredSpawnState`/`SpawnRecord` split is intentional (prompt lives in
  `starting-prompt.md`; `state/spawn/AGENTS.md:12-16`) — share a base, don't merge them.
- Findings [50], [19], [40] (dropped by adjudication); [29] (reclassified dead-code,
  separate workstream); [31] (narrowed, cancellation workstream); [34] (H-cluster).
- Store scaling/indexing — PR #376 territory.

## Sequencing

Issue 1 is the base: [72]'s composition requires [68]'s typed stored model, so they land
together and first. Issue 2 builds on 1 (facts sub-models slot into the typed model).
Issues 3, 5, 6 are independent and mergeable in any order; 3 is smallest and
first-mergeable. Issue 4 follows 1 (it types fields of the same persisted models).
Coordinate: issue 4 with PR #378 (session-identity fidelity) and issue 5 with PR #375
(streaming cleanup) — same files, different concerns; land whichever is ready first and
rebase the other.

## Work Item

thermo-nuclear-audit

## Verification

Sol reviewer ran runtime probes on `main@f9cf4a03` (Pydantic validation behavior,
nonterminal finalize acceptance, cross-harness event collisions, work-store crash
sequences); adversarial refute pass opened every cited file:line. Implementation carries
its own tests per `tests/AGENTS.md` — the crash-ambiguity and quarantine paths reproduced
by the probes become regression tests.

## Closes

Closes #400 Closes #401 Closes #402 Closes #403 Closes #404 Closes #405
