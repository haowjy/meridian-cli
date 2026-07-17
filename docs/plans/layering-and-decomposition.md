# Layering & decomposition workstream — structural cleanup map

Priority: **med**. Part of the thermo-nuclear audit (#389).
Clusters: E (layering cycles), F (god files), G (config single-parse), I survivors (dup/dead code).
Every claim below is the finder claim as corrected by both verification passes (refute + sol).

## Why

Four verified clusters describe one failure mode: the documented concentric layering
(`src/meridian/AGENTS.md:14-27` — surfaces → ops → launch → harness/state, "data flows inward")
is contradicted by the code in every measured direction, and the largest files sit exactly on
the broken boundaries:

- harness ↔ launch is a full bidirectional cycle: **68 import statements / 38 files upward,
  47 / 22 downward** (independently re-measured twice).
- state calls up into ops to locate its own files, papered over with function-local imports
  (`state/paths.py:327,334`).
- `lib/core` — everyone's bottom layer — hosts a 1120-line application service importing
  harness, launch, state, and streaming (`core/spawn_service.py`).
- one `prepare_for_runtime_write()` performs **21 TOML parses of 3 physical files**
  (runtime-instrumented), and CLI bootstrap then throws the result away so handlers redo it.
- **14 files exceed the 1000-line threshold, 20,140 of 99,632 src lines (20.2%)** [47].

The bug classes to eliminate by construction: cycle-hiding deferred imports (a new one can
always be added while the cycle exists), duplicate policy that drifts (primary `--fork`
already lost skills inheritance this way), per-layer config re-parses (every new layer adds
another read), and god-files whose invariants need a 6-row table to state.

## Goal

Make the AGENTS.md layer diagram true by construction: shared contracts owned at the bottom,
application composition in its own tier, config/context resolved once per invocation and
threaded, god-files split along their already-documented seams. Deferred/TYPE_CHECKING
cycle-dodging imports become unwritable; duplicate validation/policy sites become deletable.

---

## 1. Dependency direction

### 1a. launch ↔ harness: break the cycle by ownership-split of the contract — [43] + [44]

Evidence: `harness/adapter.py:21-33` (module-level imports of launch.composition/launch_types/
request); `launch/launch_types.py:17` (mutual SpawnParams reference); `harness/common.py:12`
(launch filename constants); `state/history.py:11`, `streaming/spawn_manager.py:22` (third
parties reaching into both sides).

Corrected remedy (sol on [43] — **not** a wholesale move into core): the mixed files cannot
move as-is (`launch_types.py:98-132` carries per-harness fields; `constants.py:34-68` mixes
artifact filenames with harness commands; `SpawnParams` drags `PermissionConfig` and
`PiHarnessProfileConfig`). Split by **ownership**:

- neutral events (`HarnessEvent`) + base launch contracts → a dependency-light contracts
  module at the bottom of the graph;
- artifact filename constants (`HISTORY_FILENAME`, `OUTPUT_FILENAME`) → state artifact
  vocabulary;
- harness commands, timeouts, adapter-specific spec fields → the harness packages.

What becomes unwritable: a harness module importing launch, and vice versa — after the split
the boundary can be lint-enforced. Gated by 1c (core must stop hosting an app service first).

Harness-specific logic evicted from launch/ in the same pass [44]:
`launch/claude_session_access.py` (Claude-only, consumed by `harness/claude.py:65` — moves
cleanly, imports only a DTO); Pi failure extraction in `launch/report.py:146-247` (imported
*upward* by `harness/extractors/pi.py:23` — canonical home is the Pi extractor); Pi
env/session-dir/timeout logic in `launch/env.py:122-163`, `resolve.py:349-451`,
`process/runner.py:189-231`; delete the `streaming_runner.py:250-257` identity wrapper.
Caveat (sol): OpenCode workspace projection in launch is a **documented** Python 3.14
eager-bootstrap workaround (`harness/.context/CONTEXT.md:250-263`) — moving it back requires
first removing/isolating the eager `harness/__init__` bootstrap; do not naively relocate.

### 1b. state owns runtime-root selection — [13] merged into [45]

Evidence: `ops/runtime.py:22` imports `state.paths` at module level while `state/paths.py:327,334`
imports `ops.runtime` function-locally; `lib/state/AGENTS.md:73-84` documents the resolvers as
state-owned. The resolution itself is built entirely from state/config primitives
(`ops/runtime.py:175-262`) — nothing ops-y.

Corrected remedy (both verifiers): [13]'s "make callers call ops directly" is refuted —
state-internal callers exist (`session_store.py:710`, `spawn_scope.py:36`), so that would
relocate the inversion, not delete it. Move **runtime-root read/write selection** down into
state/ (a dependency-light selector; `state/paths.py` already imports lib/config, so no new
edge). Keep invocation/project/config composition (`OperationRuntime`, sinks, `build_runtime`)
in ops. If hooks need the authority data shape, put the contract in a neutral module — the
hooks imports are type-only, not proof of the cycle. The function-local imports get deleted;
their reappearance is the regression signal. Adjacent: `launch/plan.py:10` imports
`OperationRuntime` upward — pass a narrower launch runtime contract instead (composition-root
work, see 2b).

### 1c. Application services out of lib/core — [46]

Evidence: `core/spawn_service.py` = 1120 lines, module-level imports of harness, launch (5
modules), state, and streaming (`signal_canceller`, line 61 — a real runtime edge, not
TYPE_CHECKING); mutual TYPE_CHECKING cycle with `streaming/spawn_manager.py:54`.
`core/AGENTS.md` documents it as an intentional exception — the exception *is* the finding.

Corrected remedy (sol): move `spawn_service.py` and the orchestration half of
`core/lifecycle.py` (1096 lines) to a **dedicated application tier (`lib/application/`), not
ops/** — routing through ops would create new launch/streaming → ops edges. Keep pure
lifecycle decisions (state machine) in core. Add streaming/ to the AGENTS.md layer diagram.
This gates 1a: the shared contract can only live at the bottom once core is actually the bottom.

### 1d. ops → cli → ops inversion — [49], CONFIRMED by both verifiers unmodified

Evidence: `lib/ops/init_ops.py:82-85,269` imports from `meridian.cli.mars_passthrough`, which
imports `resolve_mars_executable` back from `lib/ops/mars.py:31` — a literal round trip.
`cli/mars_passthrough.py` is 176 lines of subprocess mechanism, not presentation.

Remedy: move parse/execute (request/result models, env construction, subprocess run) into
`lib/ops/mars.py`; cli keeps argv splitting, stdout/stderr forwarding, SystemExit. Simpler
still (sol): let `init_ops` call a direct `execute_mars_json()` operation.

### 1e. plugin_api facade closure — [51]

Evidence: `plugin_api/git.py` is the *implementation* home of
`resolve_clone_path`/`normalize_repo_url`; internal code imports it (`launch/context.py:68`,
`context/resolver.py:16`) — external facade load-bearing for internals.

Corrected remedy (sol): move the **full dependency closure** — URL normalization, slugging,
override loading, clone-path resolution, including the `plugin_api.config`/`plugin_api.state`
dependencies — into lib; moving only `resolve_clone_path` preserves the reverse edge
indirectly. `plugin_api/git.py` becomes a re-export like its `state.py`/`fs.py` siblings.
Reconcile `plugin_api/AGENTS.md:32`'s "no lib.* dependency" claim.

### 1f. Harness-neutral continuation enrichment in ops — [38] + [4]

Evidence [38]: `ops/reference.py:254,260,298,344` branch on literal harness names (plus a
fifth instance the finder missed: `ops/session_target.py:341` opencode), against
`ops/AGENTS.md`'s "policy is harness-agnostic".
Evidence [4]: `ops/spawn/query.py:12` imports `PI_PHASE_EVENT_TYPE` into ops;
`:400-432` and `:435-511` each fully re-read the same `history.jsonl`; `detail_from_row()`
(:542-611) calls both unconditionally for every harness. (Corrected: the scan runs once per
returned detail per `spawn_wait` invocation, not per poll tick — real waste, smaller
multiplier than filed.)

Corrected remedy (both verifiers): do **not** add Pi-named capability flags — that encodes Pi
into the shared model and moves the conditional. Use a neutral adapter operation
(continuation-source enrichment) or persist adapter-owned continuation metadata at
session/spawn creation. First evaluate *deleting* the legacy Claude child-cwd fallback (no
backcompat requirement). For [4]: one single-pass Pi telemetry extractor owned by the harness
layer behind a surface-neutral seam; non-Pi rows skip the read entirely, gated through the
adapter — a literal `row.harness == "pi"` in ops would reintroduce [38].

---

## 2. God-file decomposition

Census framing [47]: 14 files > 1000 lines (counts verified to the digit), 20.2% of src.
Line count is prioritization, not a plan — the four below have real documented seams.
Connection files (`codex_ws.py` 1449, `opencode_http.py` 1325) are addressed via 4a's
lifecycle-ownership move, not an artificial split.

### 2a. ops/spawn: api.py family split + models.py render extraction — [0], [1], [2]

`api.py` = 2198 lines, 72-74 top-level defs, clean verb-family boundaries (create 266-519,
query 520-1011, cancel 1014-1392, wait 1393-1848, fork/continue 1849-2198). Split into
`api_create/api_query/api_cancel/api_wait/api_replay`, `api.py` as facade. Two corrections:
the `execute.py` precedent is **partial** (it still implements `execute_spawn_background/
blocking`), and shared authority/runtime helpers must land in a dedicated internal module —
not duplicated or cross-imported among the five family modules. Test-seam note: existing
tests monkeypatch `meridian.lib.ops.spawn.api`; the split changes that patch seam and needs
deliberate test updates. Fold in [2] (as adjusted): the `failure_payload = [payload]`
one-element mutable cell (`api.py:400-410`, mutations :310-322) — carry the latest resolved
payload as typed exception/result context from the preparation scope instead; the
`_prepare_spawn_create_with_expected_failures` wrapper can disappear with it.

`models.py` = 1405 lines; `SpawnActionOutput`/`SpawnDetailOutput` carry ~576 lines of render
bodies; a 9-line task-dir block is byte-identical twice within one class (:376-384, :438-446)
and `_has_distinct_task_cwd` is defined twice (:199, :869). Extract render bodies into a
spawn render module mirroring `session_log_render.py` — but **keep one-line
`format_text`/`to_*_wire` protocol delegators on the models** (CLI dispatch depends on the
object protocol, `cli/output.py:27-37`); separate wire projection from text rendering
internally; the duplicated helpers collapse to one.

### 2b. launch/context.py prepare/bind split — [8] (+ [9] runner decomposition)

`context.py` = 2183 lines, 45 top-level defs; `bind_launch_context()` = 407 lines
(1660-2066) doing workspace projection, permission projection (two Claude rewrites, not
three), headless denial, artifact materialization, argv, and env assembly in one body. Split
along the prepare/bind phase boundary its own `launch/AGENTS.md:21-57` documents; decompose
bind into those named steps. Correction: move only **dependency-light contracts** into
`launch_types.py` — `ChildEnvContext` is behavior-rich and stays; don't make launch_types the
next dumping ground.

[9]: `run_harness_process()` (487 function lines) and `execute_with_streaming()` (621) are
single-body orchestrators. Decompose around lifecycle stages without DTO proliferation;
streaming's strongest seams are setup / retry-attempt policy / terminal finalization
(`execute_with_streaming` side is owned by PR #375's streaming workstream — coordinate, don't
duplicate). The filed "batch the three update_spawn calls" sub-claim is **refuted**: only one
of the three writes is unconditional, ordering interleaves with `bootstrap_from_disk`, and
merging changes `spawn.updated` observer emission counts — do not ship it as filed.

### 2c. config/settings.py three-part split — [21] (+ [20])

1664 lines: source normalizers (~63-997), Pydantic models (999-1549), `load_config` (1551+).
Corrected remedy (sol **disproved wholesale TypeAdapter replacement** by runtime probe:
sparse-layer first-wins merge breaks without `model_fields_set` filtering; unknown-key
warnings vanish; `work.artifacts` gets dropped; Pi extension path items get rejected): split
into **source decoders / Pydantic contracts / loader orchestration** as three modules,
preserving the three scalar parsers as source-specific codecs (documented as such,
`config/.context/CONTEXT.md:124-134` — typed TOML vs CLI strings vs env strings differ for
cause). Centralize only the genuinely shared semantic constraints: verbosity is validated in
**four** places, autocompact minimum duplicated against `core/overrides.py:17-45`.

[20] as corrected: the `[context]` normalizer is **not dead** — `ops/config.py:337-380,526-547`
feeds `meridian config show` precedence rows from it (tests protect this). Do not flip
`merge_kind` to external. Narrow fix inside the split: stop feeding `[context]` into the
`MeridianConfig` source payload (so malformed `[context]` can't fail unrelated
`load_config()` calls), keep the inspection projection, delete the zero-caller public wrapper.

### 2d. git_autosync: context-managed stash + phase extraction — [24]

1322 lines; `_sync()` = 419 lines (720-1138) with numbered step comments 3-12; 27
`outcome="skipped"` literal constructions; conflict handling 3 levels deep (~967-1084).
Corrected remedy (sol): **first** make excluded-file stash cleanup a context-managed
invariant — that deletes the repeated cleanup branches rather than moving them — then extract
recovery / commit / merge-conflict / push phases around a small typed progress state (phases
must carry branch/divergence, commit/merge state, file stats). Typed skipped-outcome
constructor (classmethod or explicit params, not `**extra`). Preserve the clone-target lock
ownership: remote autosync calls `_sync()` while holding it (:153-172,216-272); local doesn't.

---

## 3. Config parsed once per invocation — cluster G as corrected

The corrected magnitude (runtime-instrumented by sol): `prepare_for_runtime_read()` = **12
TOML parses**, `prepare_for_runtime_write()` = **21 parses** of 3 physical files (6 full
context-config passes × 3 files + `load_config`'s 3); with the bootstrap-discard bug the
spawn path pays **24/42**. Every needed seam already exists — this is threading, not new
machinery. Both verifiers **reject process-global memoization** (env state, mutation
boundaries, long-lived service processes); the target abstraction is an invocation-scoped,
immutable prepared context.

- **[10] — the highest-leverage change; halves everything downstream.**
  `cli/bootstrap.py:313-368` builds a full typed `RuntimeRead/WriteContext` then returns only
  `project_root`; handlers rebuild (`cli/spawn.py:106-111`, `ext_cmd.py:46`,
  `streaming_serve.py:51`). `lib/bootstrap/AGENTS.md` already says prepared contexts should
  be carried downstream. Remedy: return a typed bootstrap result; install the prepared
  context in an invocation ContextVar; handlers lazily prepare only when no successful
  startup context exists (bootstrap swallows errors at :367-368 — reuse only on success; ext
  list/commands and agent-render/help paths legitimately skip bootstrap).
- **[22]** — six independent context-config passes inside one write preparation
  (`project_state.py:55-95` explicit loads; implicit reloads via `resolve_project_paths*`,
  `ensure_gitignore`, authority resolution `ops/runtime.py:228-261`). Remedy (sol): resolve
  one `ProjectLayoutSnapshot` (ContextConfig + ProjectPaths) and pass it through authority
  and all ensure operations — threading into only `ensure_project_dirs`/`resolve_layout`
  leaves the other rereads intact. The seam exists:
  `resolve_project_paths_from_context(context_config=...)` (`paths.py:251-253`). Also remove
  the `context_config=None` ambiguity ("known absent" vs "reload").
- **[7]** — `bind_launch_context()` is documented "cheap: microseconds"
  (`launch/AGENTS.md:54-57`) but parses up to 5 TOML files per call
  (`context.py:1683-1686`) and runs twice per primary launch (unconditional preview bind
  `launch/__init__.py:198-208` + real bind `process/runner.py:1013-1027`). Remedy: carry
  `ContextConfig` + `WorkspaceSnapshot` on `PreparedLaunchSurface`/`PreparedPolicySurface`
  (their documented purpose is spawn-stable resolution) — makes the AGENTS.md claim true
  instead of false. Note: `bootstrap/config.py::load_context_snapshot()` has zero callers —
  dead, deletable.
- **[25]** — lifecycle service + eager HookDispatcher (3 TOML files + hook_state.json) built
  **three times in both foreground and background** spawn creation
  (`execute_init.py:211-234,279-296`, `execute.py:472-479`, plus
  `streaming_runner.py:1089-1092` — sol found the fg third construction); the reserve-path
  dispatcher is provably unused (`lifecycle.py:248-250` never dispatches). Remedy: thread one
  lifecycle service through reserve → announce → running transitions; make dispatcher
  construction lazy until the first actual dispatch.
- **[57]** as corrected — `load_config()` is uncached and two settings helpers reload on
  fallback branches, but the mainline spawn multiplier is ~2 and is *owned by [10]* (ops/
  launch sites are guarded fallbacks reusing prepared/snapshot config). The residual: primary
  launch loads 3× (startup bootstrap, `launch/plan.py:106-123`,
  `cli/primary_launch.py:28-40` headless warning) — thread the prepared `MeridianConfig`
  into both; keep lazy fallback for standalone library callers only.

What becomes unwritable: a handler or launch phase that re-reads config from disk — everything
downstream of bootstrap consumes the invocation snapshot or doesn't compile against the seam.

---

## 4. Duplication & dead-code sweep — cluster I survivors

([50], [19] dropped by adjudication; [29] reclassified dead-code; [2] folded into 2a.)

- **[5]** managed-backend teardown/diagnostics duplicated across `codex_ws.py` and
  `opencode_http.py` (~50-55 lines: identical `_looks_like_address_in_use`,
  `_read_startup_stderr_excerpt`, `_close_log_handles` — pi_rpc.py has a third near-copy —
  port pick, terminate sequence). Corrected remedy (sol): not a utility grab-bag — make
  `managed_backend.py` own the **lifecycle**: atomic launch (today `launch_managed_backend`
  does fallible post-spawn work at :62-119 with no cleanup on partial failure), termination,
  parent-death-link release, startup-log state; adapters keep one handle instead of exploding
  it into three nullable fields (`codex_ws.py:404-406`, `opencode_http.py:526-528`). Port/
  address classification as shared utilities (don't name it "reserve" — the socket closes
  before launch).
- **[11]** fork policy duplicated-with-drift: continue already flows through the shared
  `ContinueReplayContract` (`lib/launch/continue_replay.py`), but the fork path is hand-rolled
  in both adapters (`primary_launch.py:266-323` vs `ops/spawn/api.py:1974-2101`, ~60-80 lines,
  not 150) and **has already drifted**: primary `--fork` loses source skills inheritance that
  spawn fork preserves (api.py:2090-2101 vs primary_launch.py:318-323). Remedy (sol): a
  launch-owned `ForkReplayContract` beside `ContinueReplayContract`; both adapters consume it
  and project into their own request types; explicitly reconcile the divergent
  harness-mismatch semantics (effective-target preview vs explicit-flag comparison). No
  generic ops helper parameterized over both request shapes.
- **[12]** hand-synced flag tables (`cli/bootstrap.py:15-68`) vs `root()`'s 30+ Parameters
  (`main.py:269-465`): a missing value-flag entry silently shifts argv token indexing
  (probe-confirmed). The existing test parametrizes over the table itself — circular. Remedy:
  a **contract test** introspecting `root()`'s `Annotated[..., Parameter]` metadata against
  the tables (accounting for startup-only/generated flags); startup-path introspection is
  ruled out — it defeats the lazy-import design.
- **[23]** `_install_readonly_permission_field` (`safety/permissions.py:65-81`): redundant
  and bypassed on normal assignment — Pydantic `frozen=True` intercepts first; the descriptor
  only fires on an `object.__setattr__` bypass nobody performs. Delete.
- **[26]** `lib/kg/graph.py:12,46` imports markdown_it directly (forbidden by
  `lib/markdown/AGENTS.md`) and re-reads + re-parses every file a second time per `kg check`
  (:185-241 after `extract_file` at :86). Corrected remedy (both verifiers): naive
  `doc.fenced_blocks` reuse is a trap — extracted positions are frontmatter-stripped
  body-relative and `FencedBlock` has no end line. Extend the lib/markdown extraction
  contract with source-coordinate fenced spans (start+end, frontmatter offset applied) and
  retained source text/lines; thread the docs already built in `build_analysis`; delete
  `_md_parser`/`_fenced_lines`.
- **[28]** `load_mars_descriptions()` (`catalog/model_aliases.py:635-673,684`): zero callers
  repo-wide. Delete function + `__all__` entry.
- **[29]** (reclassified dead-code by adjudication): the mermaid JS validation tier is
  **unshipped** — `mermaid-validator.bundle.js` exists nowhere (working tree, git history,
  build config), so `detect_tier()` always selects python and the per-block Node subprocess
  loop is unreachable. Delete the JS tier (`validator.py:23,82-93,323-341` + the AGENTS.md
  claim of its availability) or ship the bundle; if ever shipped, batch into one Node
  invocation rather than a thread pool.
- **[30]** vestigial `search_paths` parameter (`catalog/agent.py:102-148`), discarded via
  `_ = search_paths`, never passed by any caller. Delete from both functions; keep the
  distinct `search_dirs` seam.

## Member issues

1. launch↔harness boundary: ownership-split the spawn contract, evict harness logic from launch — [43],[44]
2. Confirmed dependency inversions: state runtime-root selection, ops→cli→ops Mars loop, plugin_api git closure — [13]/[45],[49],[51]
3. Application services out of lib/core into a dedicated tier — [46]
4. Harness-neutral continuation enrichment and Pi telemetry seam in ops — [38],[4]
5. ops/spawn decomposition: api.py family split, models.py render extraction — [0],[1],[2]
6. launch/context.py prepare/bind split and orchestrator decomposition — [8],[9]
7. config/settings.py three-part split preserving source codecs — [21],[20]
8. git_autosync: context-managed stash cleanup + phase extraction — [24]
9. Config parsed once per invocation — [10],[22],[7],[25],[57]
10. Duplication & dead-code sweep — [5],[11],[12],[23],[26],[28],[29],[30]

## Out of scope

- `execute_with_streaming` internals and drain composition — owned by PR #375; issue 6 covers
  only the launch-side `run_harness_process`.
- Derived spawn index / bounded history / typed paths — PR #376 ([4]'s read-amplification
  overlap is only the double-scan fix; index design stays in #376).
- Drain-coordinator forwarding mixin ([19]) — **refuted; do not build**. Protocol typing
  already enforces the shape; a 10-method pass-through mixin is a shallow abstraction.
- Process-global config memoization — rejected by both verifiers; the boundary is the
  invocation, not the process.
- Moving OpenCode workspace projection back into harness before the eager-bootstrap cycle
  fix (documented Py3.14 workaround).

## Sequencing

1. **Issue 9 first** ([10] specifically) — halves config work everywhere downstream and is
   pure threading through existing seams. Issue 2 and issue 10's deletions are independently
   first-mergeable.
2. **Issue 3 gates issue 1**: the shared contract can only move to the bottom once
   spawn_service leaves core (ordering confirmed by both verifiers).
3. Issue 1 then enables issue 4's adapter-seam work and shrinks issue 6's import surface.
4. Issues 5, 7, 8 are independent splits; land each with its test-seam updates in the same PR.
5. After issues 1-3: add an import-direction lint (state↛ops, launch↛ops, harness↛launch,
   lib↛cli, lib↛plugin_api except sanctioned hook seam) so every fixed edge class becomes
   unwritable.

## Closes

Closes #406 Closes #407 Closes #408 Closes #409 Closes #410 Closes #411 Closes #412 Closes #413 Closes #414 Closes #415

### Sweep additions (workstream-roadmap review, 2026-07-16)

Two verified-live dedups folded in from the dissolved future-bucket containers:

- **#240** Pi projector dedup (`projections/project_pi_native_tui.py` /
  `project_pi_rpc.py`, 240/287 lines) — scope has shrunk since filing: the
  projectors already share `_guards` and `permission_flags`; extract the
  remaining common flag-alias/collision/assembly core.
- **#139** telemetry reader JSONL parsing duplicated between `read_events`
  and the `tail_events` path (`lib/telemetry/reader.py`) — one tolerant
  line-parse helper.

Closes #240 Closes #139
