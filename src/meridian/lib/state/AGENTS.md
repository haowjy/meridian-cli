# lib/state/ — State Layer

File-backed authority for all Meridian runtime state. No service or in-memory object
survives process death. SQLite is only a rebuildable projection; authoritative state
must stay recoverable from its source files.

Writes go through `atomic_write_text` (tmp+rename); a JSONL file's torn last line is
repaired before the next append (`append_durable_jsonl_lines`). State contracts: [.context/CONTEXT.md](.context/CONTEXT.md). `project_identity.py` is the only identity
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
`ops/legacy_native_import.py` takes `locks/legacy-native-import.lock` before the gate
during native session import. Cross-leaf composition (spawn lock +
projection lock) lives in `spawn_aggregate.py`, not in either leaf repository. Pending
reaper cleanup claims make its published-row deletion seam skip the row until cleanup
completes or the claim is cleared. Per-spawn mutations never take
`spawns_flock`.

**Stable lock inodes.** Lock files are unlinked only by `unlink_validated_lock()` under a
fresh, non-reentrant, validated exclusive acquisition, immediately before release (the
`lock_gc.py` seam and cleaned session locks). Never unlink at ordinary release or inside
a reentrant frame: a lock left held on the orphaned inode lets another contender validate
against a recreated inode (split-brain).

## Rules that prevent corruption

Use the locked spawn mutation seam; a preflight read cannot decide a transition.
Writers that can outlive deletion use `published_artifact_lock()` or
`mutate_published_spawn_artifact()` so they do not recreate a deleted spawn.
A launch's terminal usage must be per-launch: `reopen_spawn` folds it into
`prior_usage`, so cumulative totals would double count.

## Launch lock

`launch_lock.py` owns the non-reentrant runner-lifetime flock (`spawns/<id>/launch.lock`).
The background worker inherits the same open-file description (`dup`/`pass_fds`), then
restores close-on-exec. Release by closing descriptors; never `LOCK_UN`, which unlocks the
worker too. Harnesses never inherit it. `LaunchSlot` creates the lock before ID
allocation; `bind_staged()` returns the `LaunchHandle`. Only low-level `start_spawn`
permits lockless test seeding. For an active lock-backed row, a free lock derives success
from a durable report, cancellation from a cancel intent, or failure otherwise. Details:
[.context/CONTEXT.md](.context/CONTEXT.md).

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

A chat keeps one `(harness, native_store, harness_session_id)` binding: absent fields
can be filled, but bound fields cannot be replaced except by a store `relocate` row.
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
