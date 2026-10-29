# lib/state/ — State Layer

File-backed authority for all Meridian runtime state. No service or in-memory object
survives process death. SQLite is only a rebuildable projection; authoritative state
must stay recoverable from its source files.

Layout of the roots (user runtime `~/.meridian/projects/<id>/`, work items under the
`[context.work]` root, never the repo), identity, and file inventory:
[.context/CONTEXT.md](.context/CONTEXT.md). `project_identity.py` is the only identity
reader and writer. Resolve home with `get_user_home()` (`user_paths.py`), never
`~/.meridian/`.

## Lock Order

The one statement of cross-store lock order; other docs link here. Outer to inner:

1. history mutation gate (`locks/history-mutation.lock`, shared; restore/archive/index
   rebuild hold it exclusive)
2. `spawns_flock`, only for spawn ID allocation, initial row publication, and staging GC
3. `locks/spawns/<id>.lock` (`write_state_locked()`)
4. `locks/process-scopes/<id>.lock` (process-scope projection)

Sessions take gate shared, then `sessions.jsonl.flock`, only through
`session_store.session_writes()`; never take `sessions_flock` alone. `work-store.flock`
and the session pair are never held together, in either order: a command that changes
both runs them as sequential steps, work store first (`ops/work_attachment.attach_executions`).
The one-time native import lock precedes the gate. Cross-leaf composition (spawn lock +
projection lock) lives in `spawn_aggregate.py`, not in either leaf repository. Pending
reaper cleanup claims block published-row deletion. Per-spawn mutations never take
`spawns_flock`.

**Stable lock inodes.** Lock files are unlinked only by `unlink_validated_lock()` under a
fresh, non-reentrant, validated exclusive acquisition, immediately before release (the
`lock_gc.py` seam and cleaned session locks). Never unlink at ordinary release or inside
a reentrant frame: a lock left held on the orphaned inode lets another contender validate
against a recreated inode (split-brain).

## Rules that prevent corruption

- **Per-launch files**: reach `report.md`, `stderr.log`, `output.jsonl`, `params.json`,
  prompts, `primary_meta.json`, `process_scopes.json`, signals, `control_actions.jsonl`,
  debug and runner logs, and the harness boundary files only through `LaunchPaths`
  (`paths.py`). It is the launch's identity (`runtime_root`, `spawn_id`, `n`) plus its
  files, and only `paths.py` constructs it: `for_row(runtime_root, row)` for a published
  row, `reserved` (called only by `spawn_store`, which assigns `n`) before publication, and
  `preview` (the dry-run placeholder; only `ops/spawn/prepare.py` and `launch/__init__.py`).
  A launch's directory is `launches/<n>/` when the row has `launch` (state v4, every new
  spawn) and the spawn directory when it does not (a v3 row's launch 1).
  The code that publishes a row returns the launch it wrote (`spawn_store.reserve_launch`,
  `start_spawn`, `LifecycleService.start`) and callers carry it (`RuntimeBindings.launch`,
  `ConnectionConfig.launch`, `PreparedSpawn.launch`, `_SpawnContext.launch`); read
  surfaces build it from the row they hold. A process that adopts a launch it did not
  publish and holds only the ID reads the row once (`spawn_store.read_launch_paths`: the
  background worker). Functions take a `LaunchPaths`, never a `log_dir`/`spawn_dir` path,
  and never recover the runtime root or spawn ID from a launch directory path. Spawn-level
  files (`state.json`, `launch.lock`,
  `scope.json`, `observed-spawns.json`, `reaper_cleanup_claim.json`, the ambient `work/`
  directory, the control socket) stay joined to `resolve_spawn_dir`: nothing a user sees
  resets on a continue; code that slices a *spawn* directory (retention) stays valid.
  Per-launch name constants are private to `paths.py`; code that recognizes a name (not
  reaches a file) uses `LAUNCH_FILE_NAMES`. `meridian.dev.check_launch_paths` (fast gate)
  rejects a per-launch file name written as a literal (or a literal ending in one), a
  `LaunchPaths(...)` construction, alias or `replace` copy outside `paths.py`, a `reserved`
  call outside `spawn_store`, a `preview` call outside the two dry-run entry points, and an
  import of a private `paths.py` name.
- **Next launch**: `spawn_store.reopen_spawn(handle, runtime_root, LaunchInput)` is the only
  writer of `launch` on an existing row. It needs the launch lock (`LaunchHandle`), requires
  K's outcome, copies K's row to `launch_archive(root, id, K)` under the state lock, folds
  K's usage into `prior_usage` (`PriorUsage.plus`), and returns the new `LaunchPaths`. It
  refuses a `parent_id` different from the row's: re-parenting needs the cycle-checked
  transaction under `spawns_flock` that is not written yet. It has no CLI caller yet.
  `LaunchInput` (validates goal, resolves the runner's start time) feeds the one builder
  `_launch_fields` that `start_spawn` and `reopen_spawn` share; add a per-launch field
  there. A launch's `TerminalFacts` usage must be that launch's own (extractors subtract a
  harness's session-cumulative totals), or the `prior_usage` fold double counts.
- **Spawn updates**: every update to a published spawn goes through `write_state_locked()`
  (re-read under lock, pure mutator, atomic write). Never read `state.json` without
  `read_state()`; raw JSON skips validation and `SpawnRecord` reconstruction.
- **Writers into `spawns/<id>/`** that can outlive deletion must not recreate the
  directory: use `published_artifact_lock()` / `mutate_published_spawn_artifact()`.
- **Writes**: state files go through `atomic.py` (`atomic_write_text`, `atomic_publish_dir`,
  `append_text_line`), never plain `open()`. JSONL reads and appends go through `jsonl.py`
  (a row exists when its newline does); repair is read-then-replace, so hold the writer
  lock. Small JSON sidecars go through `json_doc.py`.
- **New persisted path fields** use `normalize_path_for_write()` (`paths.py`).
- **Read vs write roots**: read paths use `resolve_project_runtime_root()`; only write
  paths use `*_for_write()`, which mints committed IDs or materializes ad-hoc state.
- **Ad-hoc retention** (`state.adhoc_retention_days`) is user-config/environment-only;
  cwd project config must never control a machine-wide scan.
- **Persistence leaves**: the spawn repository never imports the process-scope
  projection; the projection may use repository reads.

## Launch lock

`launch_lock.py` owns the non-reentrant runner-lifetime flock (`spawns/<id>/launch.lock`).
The background worker inherits the same open-file description (`dup`/`pass_fds`), then
restores close-on-exec. Release by closing descriptors; never `LOCK_UN`, which unlocks the
worker too. Harnesses never inherit it. `LaunchSlot` creates the lock before ID
allocation; `bind_staged()` returns the `LaunchHandle`. Only low-level `start_spawn`
permits lockless test seeding. Lock-backed rows have no liveness grace: a free lock derives
terminal. Details: [.context/CONTEXT.md](.context/CONTEXT.md).

## Status and reconciliation

- Readers get status only from `spawn_status.py` (`read_derived()`, `overlay_derived()`);
  they may return in-memory terminal status but never persist or terminate processes.
- Cancel/archive/destruction selection uses stored status until dead-launch finalization
  persists an outcome. Only a `Claimed` result from `claim_dead_launch()` authorizes
  `SpawnApplicationService.finalize_dead_launch()`, the sole writer of a dead lock-backed
  launch's outcome.
- `reconcile_active_spawn()` (doctor repair, root depth only; see `core/depth.py`) persists
  the outcome first, then terminates claimed, birth-validated scopes. Unknown identity or
  access errors never authorize primary scope termination. Read-only projected status is
  not authorization to terminate.

## History index

Use `history_index.py` for filtered discovery; keep lifecycle/control decisions on direct
authoritative reads. Indexed writers publish dirty intent before mutating files; SQLite is
never a writer dependency. Never migrate an index in place: a new `SCHEMA_VERSION` or
session event version builds a new file from authority. Read
[.context/history.md](.context/history.md) before changing locks, projection, transcript
retry, ZIP retention, or restore.

## Sessions

A chat keeps one `(harness, native_store, harness_session_id)` binding;
`update_session_harness_id` returns `BindOutcome` from `native_binding.bind`, and callers
mirror its accepted ID. Per-operation session reads and all appends use the keyed seam in
`session_store.py`; multi-chat reads use one `chats_rows` pass. Unsupported-version rows are skipped with a warning; scoped appends
refuse them.

## Work items

The location says whether an item is active; `work_repository.move_work_dir()` is the one
active↔archive move (refuses when both copies exist, never deletes one). All work mutations
go through `work_repository` under `work-store.flock`.

## Subpackage

[spawn/AGENTS.md](spawn/AGENTS.md) — models, v3/v4 persistence, pure transitions.
`../harness/AGENTS.md` (native evidence at finalization), `../launch/AGENTS.md` (writes
spawn state during launch).
