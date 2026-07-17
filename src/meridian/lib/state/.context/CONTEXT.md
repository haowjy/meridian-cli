# lib/state/ — Context

## Architecture

### Dual-Root Layout

State splits across two roots:

```
.meridian/                          ← repo-local, committed scaffolding
  id                                — project UUID / three-word ID
  id.lock                           — exclusive lock for UUID generation
  work-items/<slug>.json            — mutable JSON per work item
  work-items.rename.intent.json     — crash-safe rename intent (transient)

~/.meridian/projects/<id>/          ← user runtime, never committed
  sessions.jsonl                    — all session events, append-only
  sessions.jsonl.flock
  session-id-counter                — monotonic c1, c2, …
  sessions/                         — per-session lock + lease files
  spawn-id-counter                  — monotonic p1, p2, …
  spawns/
    .staging/<unique>/              — complete unpublished spawn row
    v2-format.json                  — v2 migration marker
    <id>/
      state.json                    — authoritative spawn state (v2)
      state.lock                    — per-spawn exclusive lock for external writers
      starting-prompt.md            — prompt body (written once)
      history.jsonl                 — primary output artifact (seq-enveloped events)
      heartbeat · report.md · stderr.log · params.json · tokens.json
      inbound.jsonl                 — injected user messages
      control.sock                  — active-session control socket
  artifacts/                        — LocalStore blob store
  worktree-temp/                    — session-scoped temporary worktree records
    <safe-key>.json                 — TemporaryWorktreeRecord per session context
```

The project ID in `.meridian/id` is the key that maps to
`~/.meridian/projects/<id>/`. Projects can be moved or renamed without losing
runtime history.

### Spawn State: V2 Per-Spawn Files

Since 2026-05, spawn state lives in individual `state.json` files under
`spawns/<id>/`, not a global `spawns.jsonl` event log.

Why: the global log had grown to 189 MB / 35,000 events. Every status read was
O(n) replay of the entire file. Primary launch had degraded to 12–13 seconds.
Per-spawn `state.json` makes reads O(1). After migration: 0.67s launch time.

Migration from the legacy global `spawns.jsonl` to per-spawn `state.json` is complete.
All active installations use v2. The `spawns.jsonl` path still exists in `RuntimePaths`
as a field but is unused by the spawn store.

### Session State

Sessions remain event-sourced JSONL (`sessions.jsonl`). The session log is
substantially smaller than spawn history and does not suffer the same O(n)
performance problem. No v2 migration for sessions.

## Contracts

### Two-Tier Write Model

Two write tiers based on who holds the lock:

**Tier 1 — Owner writes (unlocked, write-through):**
The spawn's own runner calls `write_state()` without acquiring the per-spawn lock.
It is the sole writer while active; no contention. `write_state()` performs a
best-effort terminal monotonicity guard (reads current state before writing; refuses
to overwrite an already-terminal record unless `allow_terminal_overwrite=True`).

**Tier 2 — External writes (per-spawn `state.lock`, read-merge-write):**
The reaper, cancel command, and any other process that needs to mutate a spawn it
doesn't own calls `write_state_locked()`. This acquires `spawns/<id>/state.lock`,
reads current `state.json`, applies a mutator function, and writes atomically.
The pattern prevents torn writes when multiple processes compete on the same spawn.

The distinction is enforced by convention, not runtime enforcement. If an external
writer skips `state.lock`, it races with the owner's unlocked writes.

### Atomic Write Contract

All state files are written through `atomic.py`:

- `atomic_write_text()` / `atomic_write_bytes()`: write to a same-directory temp
  file, `os.fsync()`, then `os.replace()` (atomic rename). On POSIX also fsyncs
  the parent directory. Either the old file or new file exists — never a partial
  write.
- `atomic_publish_dir()`: require a nonexistent destination, rename a complete
  same-volume directory into place, then fsync the destination parent.
- `append_text_line()`: opens in binary mode so `\n` is never translated to `\r\n`
  on Windows. JSONL byte offsets must be stable across platforms.

Never write state files with plain `open()` + `write()`. Crash in the middle of a
plain write leaves a partial file; partial state.json will fail Pydantic validation
on next read.

`start_spawn()` writes and syncs `starting-prompt.md` followed by `state.json` under
`spawns/.staging/<spawn-id>-<pid>-<random>/`, then publishes the complete row with one
directory rename while holding `spawns_flock`. Runtime-write startup removes abandoned
`.staging/*` entries under the same lock; it never garbage-collects published rows.

### Read vs Write Root Resolvers

`paths.py` provides two resolution functions. Use the right one:

| Resolver | Creates UUID? | Use when |
|---|---|---|
| `resolve_project_runtime_root()` | No | Read paths (list, show, status) |
| `resolve_project_runtime_root_or_none()` | No | Read paths where caller needs to know if uninitialized |
| `resolve_project_runtime_root_for_write()` | Yes (under lock) | Write paths (start spawn, record session) |

Using `*_for_write()` on a read path creates `.meridian/id` in untouched checkouts
(CI, first-time runs). This triggers project setup side effects unexpectedly.

### Monotonic ID Generation

**Spawn IDs** (`spawn-id-counter`): incremented under `spawns_flock` at reservation
time. IDs can be reserved before the spawn row exists (`reserve_spawn_id()`).
Format: `p1`, `p2`, `p3`, …

**Session IDs** (`session-id-counter`): incremented under `lock_file()`. Format:
`c1`, `c2`, `c3`, …

**Project UUID / three-word ID** (`.meridian/id`): generated under `id.lock` with
double-checked locking. Collision-checked against existing
`~/.meridian/context/<id>/` and `~/.meridian/projects/<id>/` directories.
Up to 10 retries; raises `RuntimeError` if exhausted.

### Terminal Write Authority

`spawn/terminal_policy.py:decide_terminal_write()` implements the projection
authority rule: a runner-origin terminal write supersedes a reconciler-origin write
on the same spawn. Terminal statuses are `succeeded`, `failed`, `cancelled`, and
`timed_out`; `timed_out` is a terminal failure class distinct from generic
`failed`. The reaper checks authority before finalizing — it will not overwrite a
spawn that the runner already terminated with a higher-authority origin.

### Reaper Behavior

`reaper.py:reap_spawns()` runs on every read path (list, show, wait, dashboard) but
only when `MERIDIAN_DEPTH` is absent, empty, or `"0"`. Nested processes and
malformed depth values fail closed (no reap side effects).

Liveness check sequence per active spawn:
1. Skip if status is already terminal.
2. Skip if not a root-side-effect process (`is_root_side_effect_process()`).
3. Skip if heartbeat age < 120s (recently alive).
4. If status = `finalizing`: prefer durable report / cancel precedence and recorded
   `runner_exit_status`; if neither proves a terminal outcome and activity is stale,
   mark failed (`orphan_finalization`).
5. If `runner_exit_status` is already recorded outside `finalizing`, preserve the
   runner's terminal tuple after the short post-runner-exit grace.
6. If status = `running` or `queued`: check if `runner_pid` is alive (using
   `liveness.py:is_process_alive()` with PID reuse guard via recorded start time).
   If dead, check completion/cancel precedence, recent activity, startup grace, and
   finally mark failed (`orphan_run` or `missing_runner_pid`).

`spawn_report_has_durable_completion(runtime_root, spawn_id)` reads `report.md` and returns True
for non-empty report content that is not a terminal control frame (`cancelled`/`error`
JSON) and is not a `# Spawn failed` generated markdown wrapper. Used by both reaper
and cancel convergence paths.

`_completion_or_cancel_decision()` centralizes durable-completion-vs-cancel precedence
for the reaper: if a durable report exists, the spawn resolves `succeeded` regardless
of cancel intent; otherwise pending cancel intent resolves `cancelled` with the intent's
exit code and error. This replaces the earlier pattern of returning
`FinalizeSucceededFromReport` or `_finalize_from_cancel_intent_decision()` directly —
violating the rule can let a late cancel downgrade a completed spawn.

**Managed-primary orphan cleanup:**

When a spawn is flagged as a potential managed primary (Codex / OpenCode kind=primary)
and must be finalized as failed, `reconcile_active_spawn()` first uses recorded
`process_scopes.json` cleanup, then attempts managed-primary cleanup before writing
the terminal state. The managed tier used depends on how much metadata is readable:

1. **Managed snapshot available** (`read_managed_primary_snapshot()` succeeded):
   `terminate_managed_primary_processes(managed_snapshot.metadata)` — terminates
   launcher, backend, and TUI PIDs tracked in the snapshot.

2. **Managed snapshot missing, metadata readable via late read**
   (`read_primary_metadata()` on the spawn directory succeeds):
   `terminate_managed_primary_processes(metadata)` — same termination path from
   a fresh metadata read.

3. **Metadata unreadable** (both snapshot and late read fail):
   recorded scope cleanup already ran before this branch, cleaning
   up what can be cleaned via scope records. A warning is logged; no further action
   is taken because all available cleanup mechanisms have already fired.

## Patterns

### Platform Locking

Use `platform.locking.lock_file(path)` for all cross-process locking:
- POSIX: `fcntl.flock(LOCK_EX)` — advisory, kernel-backed
- Windows: `msvcrt.locking()` with retry loop (50 ms sleep)

Thread-local reentrancy: a thread that already holds the lock can re-enter on the
same path without deadlocking. Do not use `threading.Lock` or `fcntl` directly —
the platform module handles both OS and thread-reentrancy.

### Work Item Store Pattern

Work items use a different pattern from spawns: **one mutable JSON file per item**
under `work-items/<slug>.json`. Mutable JSON is appropriate here because work items
are correlated with a directory that moves on rename — event-sourcing would add
complexity without benefit.

Rename is crash-safe: `work-items.rename.intent.json` is written before any rename
begins. Leftover intent is replayed on startup/reconciliation.

### Temporary Worktree Store Pattern

`temp_worktree_store.py` stores session-scoped managed worktree records keyed by
spawn_id or chat_id. Path: `~/.meridian/projects/<uuid>/worktree-temp/<safe-key>.json`.

`TemporaryWorktreeRecord` fields: key, repo_path, worktree_name, worktree_path, branch,
status (pending|ready), managed, updated_at. Status field supports crash recovery:
`pending` means provisioning was interrupted; `ready` means available.
`get_temporary_worktree_status()` in `ops/worktree_ensure.py` heals pending-but-dir-exists
records by updating status to ready.

API: `get_temporary_worktree()`, `put_temporary_worktree()`, `clear_temporary_worktree()`.
All writes use `atomic_write_text()`.

### WorktreeMetadata: Path Assignment vs Managed Ownership

`WorktreeMetadata` in `work_store.py` separates **path assignment** from **managed
git-worktree ownership** via the `managed` flag:

- **`managed=True`**: worktree was provisioned by `work start` through
  `provision_for_start()`. The lifecycle layer (`ops/worktree_lifecycle.py`) owns
  cleanup on done/delete, rename, and restore on reopen.
- **`managed=False`**: path was set manually via `work set-worktree`. Lifecycle
  operations skip it — `cleanup_for_done()`, `cleanup_for_delete()`, and
  `rename_worktree()` all return `skipped_manual` for unmanaged worktrees.

This separation prevents destructive lifecycle operations from removing or moving
directories that the user explicitly pointed a work item at. Shared worktree
references (multiple items referencing the same path) also block cleanup via the
`shared_with` guard in `cleanup_for_done()` and `cleanup_for_delete()`.

The `pending` flag supports crash recovery: set before `git worktree add`, cleared
after. `recover_pending()` in `worktree_lifecycle.py` heals or clears interrupted
provisions.

**Path separator normalization**: `WorktreeMetadata.path` and `.repo_path` normalize
backslash separators to POSIX (forward slash) at the Pydantic validation boundary via
`@field_validator(..., mode="before")`. The coercion function `_coerce_worktree_metadata()`
also detects separator normalization and marks legacy records for rewrite. This ensures
stored metadata is stable when written on Windows and read elsewhere.

### User-Level Storage for New Features

New features that need user-level storage (git clones, cache, custom data) go under
`get_user_home()` from `user_paths.py`. Do not hardcode `~/.meridian/` or introduce
new `LOCALAPPDATA` / `XDG_DATA_HOME` branches. `get_user_home()` handles all
platform variants correctly.

### Anti-Patterns

**Don't read `state.json` without `read_state()`** — raw JSON reads bypass Pydantic
validation and miss the `SpawnRecord` reconstruction from `starting-prompt.md`.

**Don't use `*_for_write()` on read paths** — it creates the project UUID in clean
checkouts, triggering project setup side effects in CI.

**Don't use `open()` for state file writes** — use `atomic_write_text()` or
`append_text_line()`. Plain writes don't survive crashes.

**Don't acquire `spawns_flock` for per-spawn mutations** — the global lock serializes
spawn ID allocation, initial row publication, and abandoned-stage GC. Acquiring it for
later individual mutations creates unnecessary contention. Use `write_state_locked()`
(per-spawn `state.lock`) for external writes.

## Related KB

> KB lives at `$MERIDIAN_CONTEXT_KB_DIR` (see `meridian context kb`).

- `$MERIDIAN_CONTEXT_KB_DIR/architecture/state-system.md` — full dual-root layout,
  v2 migration rationale, session state, work item store, read vs write resolution
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/spawn-finalization.md` — terminal write
  authority lattice, how finalization interacts with the reaper
## Related .context/

- [../../harness/.context/CONTEXT.md](../../harness/.context/CONTEXT.md) — `ArtifactStore` protocol that reads from `artifact_store.py`; `SpawnExtractor` contract
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — launch pipeline that writes spawn state via `SpawnStore`; composition seam, prepare/bind split
- [../../platform/.context/CONTEXT.md](../../platform/.context/CONTEXT.md) — `lock_file()`
  implementation details, Windows/POSIX branching
