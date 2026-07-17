# lib/state/ — Context

## Architecture

### Dual-Root Layout

State splits across two roots:

```
.meridian/                          ← repo-local, committed scaffolding
  id                                — project UUID / three-word ID
  id.lock                           — exclusive lock for UUID generation

~/.meridian/projects/<id>/          ← user runtime, never committed
  sessions.jsonl                    — all session events, append-only
  sessions.jsonl.flock
  session-id-counter                — monotonic c1, c2, …
  sessions/                         — per-session lock + lease files
  spawn-id-counter                  — monotonic p1, p2, …
  spawns/
    .staging/<unique>/              — complete unpublished spawn row
    <id>/
      state.json                    — authoritative spawn state (v2)
      state.lock                    — per-spawn exclusive lock for external writers
      starting-prompt.md            — prompt body (written once)
      history.jsonl                 — primary output artifact (seq-enveloped events)
      heartbeat · report.md · stderr.log · params.json · tokens.json
      inbound.jsonl                 — injected user messages
      control.sock                  — active-session control socket
  artifacts/                        — LocalStore blob store

<context.work root>/<slug>/         ← context-resolved, not repo-local
  __status.json                     — mutable per-work-item metadata
  prompts/ handoffs/ …              — work artifacts
```

The project ID in `.meridian/id` is the key that maps to
`~/.meridian/projects/<id>/`. Projects can be moved or renamed without losing
runtime history.

### Spawn State: V2 Per-Spawn Files

Spawn state lives in individual `spawns/<id>/state.json` files so status reads are
O(1) instead of replaying an O(n) global event log.

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

### Reconciliation Behavior

`reaper.py:reconcile_spawns()` is a read-only batch projection used by list, stats,
reference-discovery, and dashboard callers. For each active row it calls
`peek_reconciled_active_spawn()`, which may return an in-memory terminal view but
does not persist state, terminate process scopes, or check process depth.

`reconcile_active_spawn()` is the mutating repair operation used by doctor orphan-run
repair. It fails closed unless `is_root_side_effect_process()` is true, applies the
same reconciliation decision rules, cleans recorded orphan scopes, and persists a
terminal result through the locked external-writer path.

Shared liveness decisions skip recent activity and live runner PIDs; prefer durable
report completion, cancel precedence, and recorded runner terminal tuples; and
otherwise classify stale active rows as orphan failures after startup/finalization
grace periods. PID checks use the recorded creation time to guard against reuse.

`spawn_report_has_durable_completion(runtime_root, spawn_id)` reads `report.md` and returns True
for non-empty report content that is not a terminal control frame (`cancelled`/`error`
JSON) and is not a `# Spawn failed` generated markdown wrapper. Used by both reaper
and cancel convergence paths.

`_completion_or_cancel_decision()` centralizes durable-completion-vs-cancel precedence
for the reaper: if a durable report exists, the spawn resolves `succeeded` regardless
of cancel intent; otherwise pending cancel intent resolves `cancelled` with the intent's
exit code and error. Call this helper rather than branching on completion and cancel
independently; a late cancel must not downgrade a completed spawn.

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

`work_scope.py` resolves each work directory; `work_store.py` stores its mutable
metadata atomically in `<context.work root>/<slug>/__status.json`.

### WorktreeMetadata

`WorktreeMetadata` in `work_store.py` preserves path, branch, repository, name,
pending, and managed fields in work-item status. Current code reads the path and
pending flag for dashboard display but does not provision, rename, or clean up git
worktrees. Archiving or reopening a work item clears `pending`.

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
  per-spawn state, session state, work item storage, and read vs write resolution
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/spawn-finalization.md` — terminal write
  authority lattice, how finalization interacts with the reaper
## Related .context/

- [../../harness/.context/CONTEXT.md](../../harness/.context/CONTEXT.md) — `ArtifactStore` protocol that reads from `artifact_store.py`; `SpawnExtractor` contract
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — launch pipeline that writes spawn state via `SpawnStore`; composition seam, prepare/bind split
- [../../platform/.context/CONTEXT.md](../../platform/.context/CONTEXT.md) — `lock_file()`
  implementation details, Windows/POSIX branching
