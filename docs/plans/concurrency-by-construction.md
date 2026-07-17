# Concurrency by construction

Workstream draft — thermo-nuclear audit (#389). Priority: **high**.
Findings: cluster A ([60]+[70], [61]+[27], [62], [63], [64], [67]) + cluster B survivors ([48], [65], [66]).
All claims below are the panel-corrected versions (Fable refute + sol reviewer); refuted details are not restated.

## Why

Meridian is a multi-process coordinator whose only authority is files, yet almost every
store mutates those files as unlocked read-modify-write: the spawn record has a documented
"owner tier" that whole-file-replaces from a stale in-memory cache
(`lib/state/spawn/repository.py:260-291`, `lib/core/lifecycle.py:733-747`), the archive
set spans two separate flock acquisitions (`lib/spawn/archive.py:52-56`), the work store
takes its flock in exactly one of its ~11 mutating functions (`lib/state/work_store.py:599`),
and hook interval state is a load-once dict rewritten whole (`lib/hooks/interval.py:38-40,86-90`).
Underneath, the lock primitive itself is breakable — lock files are unlinked while held
(`lib/ops/pruning.py:253-258`, `lib/state/session_store.py:816-819`) and
`platform.locking.lock_file` never revalidates inode identity (`lib/platform/locking.py:28-62`) —
and the atomic-write contract is prose (`lib/state/AGENTS.md:58-71`) with four divergent
implementations and live drift.

Verification reproduced five of these as running-code data loss: spawn metadata clobber,
archive lost update, work-item field loss, hook timestamp loss, POSIX lock split-brain.
These are not five bugs; they are one absent abstraction instantiated five times. The
codebase already contains its own correct patterns — `finalize_spawn`'s mutator-under-lock
(`lib/state/spawn_store.py:710-756`), the artifact store's single critical section
(`lib/artifact/service.py:342-426`), the session store's private inode revalidation loop
(`lib/state/session_store.py:307-343`) — each implemented once, privately, where one person
knew better.

## Goal

Make the lost-update, split-brain, kill-mid-run, and torn-write bug classes **unwritable**,
not merely fixed:

1. **One mutate-under-lock seam per store** (`mutate(key, pure_fn)` under the store's lock),
   modeled on `finalize_spawn`'s mutator pattern. Public read/write pairs that compose into
   RMW disappear from the API.
2. **Lock identity**: stable lock inodes outside destructible resource directories /
   never-unlink protocol, portable to Windows; one parameterized lock primitive (reentrancy,
   timeout, shared mode) replacing the three implementations.
3. **Reaper ordering**: finalize-first, with **at-least-once idempotent** cleanup — not
   exactly-once kill gating, which breaks crash-onlyness.
4. **One atomic-replace context manager** in a dependency-neutral platform layer, serving
   `state.atomic`, `plugin_api.fs`, autosync, and the codex streaming rewriter.
5. **A conformance guard** (AST test, not ruff TID251) that makes raw writes to
   authoritative state unmergeable.

Performance note: the seams cost nothing measurable — owner-tier spawn writes are a handful
per spawn, `state.lock` is per-spawn, and `lock_file` is thread-reentrant. The unlocked hot
path buys nothing (verified in the A refute pass).

## Member issues

### 1. Foundations (first-mergeable, independent of each other)

**Lock identity — stable inodes, one primitive** `[63]`
`lock_file` acquires with no post-acquire fstat-vs-stat check (`platform/locking.py:28-62`);
`cleanup_stale_sessions` unlinks lock files (`session_store.py:816-819`) and pruning rmtrees
spawn dirs containing `state.lock` while holding only `spawns_flock` (`pruning.py:253-258`) —
external writers serialize on `state.lock` (`repository.py:303`). Split-brain reproduced on
POSIX. Panel corrections shape the remedy: revalidation alone is insufficient (a cleaner can
unlink after another process validated), and "only unlink while holding" is not portable —
Windows requires closing handles before deletion (`session_store.py:806-813`). Target: lock
inodes live outside destructible directories (or are never unlinked; session cleanup deletes
the lease, keeps the lock inode); fold revalidation into one parameterized primitive that
absorbs `session_store`'s private copy (deleted) and `plugin_api/fs.py:15-92` (delegates —
the primitive must grow timeout + shared mode; `fs.py:12` already imports `lib.platform`, so
no new boundary breach).

**One atomic-replace primitive; fix live drift** `[48]` + `[66]` drift half
`autosync_store._atomic_write_text` (`autosync_store.py:443-446`) is fixed-tmp-name,
no-fsync — protecting exactly the conflict/sync-state records whose loss silently reopens the
push path; `harness/codex.py:91-144` duplicates `_fsync_directory` verbatim and re-implements
the replace dance (justified divergence: it streams rollout lines; `atomic_write_text` can't
substitute without buffering). Sol's correction on ownership: `plugin_api.fs` must not import
`state.atomic` (dependency direction, `plugin_api/AGENTS.md:28-32`) — the primitive is a
generalized atomic-replacement **context manager in a dependency-neutral platform layer**;
`state.atomic`, `plugin_api.fs`, autosync, and codex all delegate (codex keeps streaming
through the open handle). `autosync_store`'s stdlib-only constraint (`autosync_store.py:1-8`)
is consciously relaxed to plugin-api-only, or the writer is injected by `git_autosync` —
either preserves the standalone-plugin extraction story. Same issue fixes the confirmed
drift sites: `failure.json` raw write (`lifecycle.py:1028-1035` — best-effort diagnostic, but
wrong layer; move to a state-owned module with atomic replace) and the Windows `control.port`
file (`control_socket.py:73` — consumed cross-process by `cli/spawn_inject.py:81,109`, whose
reader fails hard on torn content; atomic publish, no fsync needed — ephemeral).

### 2. Store seams (each independent; all sit on the foundations)

**Spawn record: collapse the two-tier write model** `[60]` absorbing `[70]`
`write_state()` re-reads current state only for the terminal guard and `cancel_intent`
re-merge, then whole-file replaces from the caller's record, unlocked
(`repository.py:260-291`); lifecycle owner transitions write from cached `self._record`
(`lifecycle.py:733-747`). Any field written externally under `write_state_locked`
(`harness_session_id`, `work_id`, `desc`, `claude_config_dir`, `runner_pid` via
`spawn_store.py:453-519`) is silently reverted — live clobber path: `runner.py:1094-1099`
sets `claude_config_dir` locked, then `mark_running` (`runner.py:1153`) rewrites from the
pre-prelaunch cache. Reproduced. The `revision` field is written and never read
(`repository.py:285-286`). Remedy: every mutation goes through
`write_state_locked(id, pure_transition)` using `spawn/transitions.py`; **privatize
`write_state`**. Equivalent: revision CAS — but per sol, a CAS must re-read and reapply the
pure transition on mismatch; merely locking the stale whole-record write fixes nothing.
With the unlocked tier gone, `[70]`'s "ownership is convention" has nothing left to enforce
(both verifiers refuted the `SpawnOwnership` capability object — it cannot span the
publish-process ≠ run-process topology); the lifecycle cache demotes to a read hint and the
convention prose in `spawn/AGENTS.md` / `state/.context/CONTEXT.md:79-80` is deleted.

**Archive set: one mutate API** `[61]` absorbing `[27]`
`archive_spawn` = read (flock acquired+released, `archive.py:29`) → in-memory add → write
(flock re-acquired, `:48`); the service's per-spawn-id **asyncio** lock
(`spawn_service.py:955-971`) serializes neither different IDs nor different processes.
Lost update reproduced; breaks the documented permanent-archive invariant
(`lib/spawn/AGENTS.md`). Remedy: `mutate_archived_spawns(runtime_root, mutator)` holding
the flock across RMW; whole-set write privatized (reads may stay public — sol). Per sol,
the mutator returns whether the ID was newly inserted, and the service uses that instead of
its separate `is_spawn_archived()` check — closing the duplicate-event race in the same move.

**Work store: locked mutation seam + directory-namespace ops** `[62]` (coordinate with PR #379)
`work-store.flock` is taken only in `ensure_work_item_metadata` (`work_store.py:598-627`);
`update_work_item` (`:789-819`), `update_work_item_task_dir` (`:823-871`),
`update_work_item_worktree` (`:874-936`) do unlocked full-payload RMW (field loss
reproduced); archive/reopen/rename/delete are unlocked locate→rmtree/rename compositions
(`:939-1070`, `:1073-1106`; the both-locations state is already warned about at
`:949-952`); read paths persist coercions (`:314-316`, `:399-400`), making `work list` a
racing writer. Remedy: `_mutate_item(slug, fn)` under the flock for all status RMW **and**
directory-namespace ops; reads become pure; one explicit `heal()` under lock persists
coercions. Panel corrections bounding the design: there is **no rename-intent file** —
`state/.context/CONTEXT.md:207-213` describes the obsolete `<slug>.json` store (fix the
stale doc, don't design on it); `update_work_item_worktree` has no production caller under
`src/`; and at 1,108 lines the seam should be a focused repository module, not further
growth of `work_store.py`.

**Hook interval state: per-hook `run_if_due` seam** `[67]`
`IntervalTracker` loads `hook_state.json` once (`interval.py:38-40`) and `mark_run`
rewrites the whole cached dict unlocked (`:86-90`); one tracker per dispatcher per process
(`dispatch.py:88,247,273`). Cross-process timestamp loss reproduced — failure direction is
early re-fire, never skip. Sol's deeper cut: `should_run`/`mark_run` bracket an unlocked
execution, so two processes can both observe "due" and run the same hook concurrently even
with merged writes. Remedy: dissolve the shared dict into per-hook timestamp files
(`hooks/last-run/<name>`) — the cross-key lost-update class has nothing left to lose — and
serialize check→execute→commit per hook name behind one `run_if_due(name, fn)` seam.
(git-autosync has no default interval, `git_autosync.py:123-124`; affected only when
configured.)

### 3. Ordering and composition (on top of the seams)

**Reaper: finalize-first, at-least-once cleanup** `[64]`
`reconcile_active_spawn` kills recorded scope processes (`reaper.py:688`) before the
terminal CAS (`:689-697`), deciding from an unlocked snapshot (`:655-664`); `finalize_spawn`
can reject under `state.lock` after the kills already happened (`spawn_store.py:710-756`).
A runner finishing inside the decide→kill window still gets its scope killed; concurrent
root CLIs double-kill. Both verifiers **refuted exactly-once kill gating** (crash-only: a
crash after CAS win but before the kill leaves a terminal row the reaper skips forever,
`reaper.py:652` — orphans never swept). Target ordering: finalize first; kill whenever the
post-CAS snapshot is terminal and a recorded scope still exists; delete the scope record
only after successful termination — idempotent, birth-validated, at-least-once. Sol's
composition constraints: `complete_spawn` marks scopes released after a terminal write
(`spawn_service.py:924-933`) and cleanup skips `already_released`
(`process_cleanup.py:107-132`), so the reaper must persist a recoverable cleanup claim under
the spawn lock rather than bypassing canonical lifecycle events — a crash between claim and
cleanup must be recoverable on the next pass.

**Autosync: one transaction seam** `[65]`
Writer-set asymmetry: the hook path holds the clone-target lock with every mutation inside
(`git_autosync.py:166-172`), but the resolve path mutates the same metadata and AGENTS.md
with no lock anywhere in its caller chain (`ops/sync_conflicts.py:192-193`,
`cli/sync_cmd.py:65-67`), and local-only autosync bypasses the clone lock entirely yet
writes sync state (`git_autosync.py:159-161`, `:904-917`). The AGENTS.md notice insert/strip
(`autosync_store.py:263-358`) is RMW over a user/agent-edited file — a probe destroyed a
concurrent user edit; the fixed shared tmp path produced a rename failure under two writers.
Resolve is itself a non-atomic two-step (notice stripped before metadata marked). Remedy
(sol's shape): one autosync transaction seam — a canonical lock-path resolver owned by the
store; a transaction context held across the complete remote/local workflow **and** conflict
resolution; mutation methods available only from the transaction handle (no nested
reacquisition — `plugin_api.file_lock` is non-reentrant, probe-confirmed, so this depends on
the unified `[63]` primitive); per-conflict rewrites of user-owned AGENTS.md removed — an
autosync-owned notice file is acceptable only with an explicit ingestion contract that
agents actually see it.

**Conformance guard: raw state writes become unmergeable** `[66]` guard half (coordinate with PR #380)
The contract exists only as prose (`state/AGENTS.md:58-71`). Fable's correction kills the
ruff route: TID251 matches import-resolved qualified names and cannot flag
`p.write_text(...)` on an inferred `Path`. The workable branch is an AST conformance test
matching `.write_text(` / `.write_bytes(` / `json.dump(` attribute calls, **repo-wide with
scope derived from the drift inventory** (the finder's fixed package list omitted
`lib/telemetry`, which its own evidence cited), with a documented allowlist for legitimate
non-state writes (`telemetry/maintenance.py:67-73` mtime-only marker; `.git/info/exclude`
derived git config). Lands last: only mergeable once the primitive exists and the drift
sites are fixed.

## Resulting behavior

What becomes unwritable or deleted:

- Public unlocked `write_state` — privatized; the owner/external "convention" prose deleted.
- Public `read`+`write_archived_spawns` composition — the lost-update shape cannot be expressed.
- `session_store`'s private lock-revalidation loop — deleted; `plugin_api/fs` lock — delegates.
- `autosync_store._atomic_write_text`, codex `_fsync_directory` copy — deleted.
- `IntervalTracker`'s shared dict — dissolved into per-hook files.
- Per-conflict AGENTS.md rewrites — removed.
- New raw writes to authoritative state — CI-rejected.
- Kill-before-CAS and double-kill — collapsed into the existing lock, cleanup at-least-once.

## Out of scope

- `[50]` (JSON-read helper unification) — REFUTED by sol: the four helpers have different
  inputs and failure semantics; a shared `read_json_object` would be a leaky abstraction.
- The external-editor trust model: flocks serialize meridian processes only, same as every
  store today (noted in verification; not a regression).
- Store scaling/read-amplification — PR #376 territory (H cluster).
- Manager/adapter cancellation gap (`[31]`, narrowed) and dead JS validation tier (`[29]`) —
  other workstreams.

## Sequencing

1. `[63]` lock primitive and `[48]`+drift atomic primitive — parallel, first-mergeable.
2. Store seams `[60]`, `[61]`, `[62]`, `[67]` — parallel, each on the foundations.
3. `[64]` reaper (needs `[60]`'s seam semantics), `[65]` autosync (needs `[63]` + `[48]`).
4. `[66]` conformance guard — last, once drift is zero.

Coordination: `[62]` overlaps PR #379 (work activation touches the same store); `[66]`'s
failure.json move overlaps PR #380 (spawn lifecycle observability).

## Work item

thermo-nuclear-audit (#389)

## Verification

Planning draft. The panel already produced deterministic reproduction probes for the spawn
clobber, archive lost update, work-store field loss, hook timestamp loss, POSIX lock
split-brain, autosync tmp collision, and AGENTS.md edit loss — each should be turned into a
regression test that the corresponding seam makes pass, per `tests/AGENTS.md`.

## Closes

Closes #391 Closes #392 Closes #393 Closes #394 Closes #395 Closes #396 Closes #397 Closes #398 Closes #399
