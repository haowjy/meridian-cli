# lib/state/ — Context

## Architecture

### Dual-Root Layout

State splits across two roots:

```
meridian.toml
  [project] id                      — committed machine-managed project identity

bare path
  sha256(canonical path)            — ad-hoc identity; no file written there

~/.meridian/projects/.locks/<id>.lock
                                    — project-lifetime gate outside deletable root
~/.meridian/projects/<id>/          ← user runtime, never committed
  origin.json                       — ad-hoc attribution/activity only
  sessions.jsonl                    — all session events, append-only
  sessions.jsonl.flock
  session-id-counter                — monotonic c1, c2, …
  sessions/                         — per-session lock + lease files
  spawn-id-counter                  — monotonic p1, p2, … (stable spawns only)
  locks/spawns/<run_id>.lock        — stable per-run mutation lock
  locks/process-scopes/<id>.lock    — stable process-scope projection lock
  locks/reaper-cleanup/<id>.lock    — stable reaper cleanup lock
  locks/launch-boundary/<id>.lock   — stable launch-boundary append lock
  locks/gc.lock                     — lock-GC pass serialization
  spawns/
    .staging/<unique>/              — complete unpublished spawn row
    <id>/
      state.json                    — authoritative spawn state (schema v3)
      starting-prompt.md            — prompt body (written once)
      history.jsonl                 — legacy runner stream; no longer written, removed by session archive prune
      attempt-N/                    — legacy retry evidence; new runs do not create it
      last-observed-event.json      — legacy checkpoint; no longer written, removed by session archive prune
      runner-lifecycle.jsonl        — runner lifecycle breadcrumbs
      finalize-evidence.json        — orphan-time liveness snapshot
      process_scopes.json           — durable process identities
      reaper_cleanup_claim.json     — pending finalize-first cleanup targets
      heartbeat · report.md · stderr.log · params.json · tokens.json
  artifacts/<spawn-id>/             — auxiliary artifact storage

<context.work root>/<slug>/         ← context-resolved, not repo-local
  __status.json                     — mutable per-work-item metadata
  prompts/ handoffs/ …              — work artifacts
```

Committed project IDs map `meridian.toml` to `~/.meridian/projects/<id>/` and
survive moves. Marker-less roots use a deterministic `adhoc.<slug>.<hash>` key;
their state follows the canonical path and the source directory stays untouched.

### Spawn State: Per-Spawn Files

Spawn state lives in individual `spawns/<id>/state.json` files so status reads are
O(1) instead of replaying an O(n) global event log. The directory layout came from
the v2 migration; published rows now use schema v3.

### Session State

Sessions remain event-sourced JSONL (`sessions.jsonl`). Reads replay the
truncation-tolerant journal into current records; session browse orders and limits
those lightweight records before enriching the visible page. New primary launches
also append their canonical `spawn_id` relationship. Current recovery and transcript
target reads use it directly to avoid global spawn scans; the official SQLite index
planned in 0.4.0 can reuse the same authority. Until that index becomes the shared
state-layer read path, browse does not own a separate projection. See `.context/TODO` and
[ops/.context/CONTEXT.md](../../ops/.context/CONTEXT.md).

### One-time native identity import

Runtime authority resolution invokes `ops/legacy_native_import.py` once for an
existing sessions journal. Its atomic marker records unresolved chats; ordinary
reads never repair missing keys afterward. Import binds through the same
`session_binding.py` lock-scoped path as single-session updates. One replay per
batch avoids quadratic work; the import lock precedes the history-mutation gate
and sessions lock. Historical restored records stay inert.

The marker also drives two later, doctor/background-only passes: the late re-bind
(`late_retries`) for IDs recorded after the import, and the one-shot legacy Pi
recovery (`pi_recovery_tried`) for 0.6.7 Pi chats that never recorded an ID. Both
bind through `session_bindings`; new marker fields default for old markers.

The dev report is `python -m meridian.lib.ops.legacy_native_import RUNTIME_ROOT`.
It deliberately bypasses runtime resolution and telemetry. OpenCode validation
uses the adapter's exact `mode=ro` reader on the live database, in place; like any
SQLite reader it touches WAL shared-memory read marks, but native source bytes are
never written. A `sqlite3.Error` raises rather than reading as absent, and defers
the import without a marker. Native validation happens before taking the sessions lock; identity is
rechecked inside it and all accepted update lines share one durable append.

### Conversation model selections

`session_store` projects `model_selection` events separately from session liveness
and historical `SessionRecord.model`. The key is harness plus native conversation ID
within the runtime root; log order, not timestamps, determines the latest selection.
Only accepted-running callbacks may append an invocation selection. An initial seed
represents original legacy intent and never outranks a started invocation.

Pending selections bind through ID updates with the same captured chat generation
and startup attempt. Binding does not move the original event's position. Pair-level
invocation/conversation dedup prevents duplicate accepted-running callbacks from
promoting an old selection; separate launches can retain distinct new conversations.
Identity conflicts fail rather than moving a selection. Reads do not write seeds or consult attempted snapshots.
New-protocol starts cannot seed prelaunch intent. Launch callers must pass the
protocol marker and commit only at their accepted-running boundary.

## Contracts

### Locked Spawn Mutation

Every mutation of a published spawn calls `write_state_locked()`. It acquires the
stable per-spawn lock, re-reads `state.json`, applies a pure mutator, and writes the
result atomically. Lifecycle-owned in-memory records are read hints, never write
snapshots.

### Atomic Write Contract

All state files are written through `atomic.py`:

- `atomic_write_text()` / `atomic_write_bytes()`: write to a same-directory temp
  file, `os.fsync()`, then `os.replace()` (atomic rename). On POSIX also fsyncs
  the parent directory. Either the old file or new file exists — never a partial
  write.
- `atomic_publish_dir()`: require a nonexistent destination, rename a complete
  same-volume directory into place, then fsync the destination parent.
- `append_text_line()`: opens in binary mode so JSONL newline encoding and byte
  offsets remain stable. It is the raw primitive; durable appends go through
  `jsonl.py` (below).

### JSON Sidecar Contract (`json_doc.py`)

Small JSON sidecars (session leases, primary metadata, process scopes, cleanup
claims, spawn scopes, idle state, Pi lifecycle, autosync state and conflicts) are
Pydantic models persisted only through `read_model()` / `write_model()` /
`mutate_model()`. `read_model()` returns `None` for a missing, truncated or invalid
file, so a corrupt sidecar loads as absent and the next write repairs it.
`mutate_model()` requires the caller's lock and re-reads under it; there is no
unlocked read-modify-write. A new sidecar adopts this seam rather than hand-rolling
read, validate, lock and replace.

### JSONL Commit Contract (`jsonl.py`)

One rule for every JSONL file: **a row exists when its newline does.**

- `iter_jsonl()` yields only newline-terminated rows. A torn final row is withheld,
  not parsed, until a later append commits or repairs it.
- `append_durable_jsonl_line()` takes exactly one row, owns its newline, and rejects
  embedded CR/LF. Before appending it repairs the tail: a delimiter-less tail that
  parses as a JSON object gets its newline; a partial one is dropped. Repair failure
  propagates; never acknowledge an event appended onto invalid JSON.
  `append_durable_jsonl_lines()` is the batch form: one repair, one write, one
  `fsync` for the batch (telemetry's durability unit).
- Malformed complete rows are skipped with a debug-level diagnostic. Readers that
  must fail closed (the archive receipt catalog) pass `on_malformed="raise"`.
- Repair is read-then-replace, so callers hold the writer lock or own a
  single-writer file.

Terminal status, liveness and `spawn wait` read `state.json`, not JSONL, so a
withheld torn tail can delay an event's visibility but never hides a completion.

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
| `resolve_project_runtime_root_for_write()` | Materializes/mints | Write paths (start spawn, record session) |

Using `*_for_write()` on a read path mints a marked project's ID or materializes
an ad-hoc user-state root. Either is an unexpected read side effect.

### Monotonic ID Generation

**Spawn IDs** (`spawn-id-counter`): incremented under `spawns_flock` at reservation
time. IDs can be reserved before the spawn row exists (`reserve_spawn_id()`).
Format: `p1`, `p2`, `p3`, … The counter mints stable spawns only.
`reserve_run()` numbers later runs from the highest run ever reserved plus one,
using the durable `spawn-heads/<pN>` high-water mark.
When the counter file is missing,
recovery seeds from the numeric spawn prefix of every surviving row, dotted ones included (`p100.2` seeds 100).
Ignoring dotted rows entirely would re-mint `p100` after retention retired its
run 1.

**Session IDs** (`session-id-counter`): incremented under `lock_file()`. Format:
`c1`, `c2`, `c3`, …

**Committed project ID** (`meridian.toml` `[project] id`): generated under the
user-home project-config lock. Collision-checked against existing
`~/.meridian/context/<id>/` and `~/.meridian/projects/<id>/` directories.
Up to 10 retries; raises `RuntimeError` if exhausted.

**Ad-hoc project ID**: deterministic from the canonical source path. The
`adhoc.` prefix is reserved and its project-lifetime lock sits outside the
deletable root. Machine-wide retention scans only this namespace, rechecks
activity under the exclusive lifetime lock, and deletes matching runtime and
context roots together.

### Terminal Write Authority

Terminal write authority is inlined in `spawn_store.py:finalize_spawn()`. The
finalize mutator checks `current.terminal.origin` against the incoming origin
under the per-spawn lock: a reconciler-origin terminal write can be replaced by
an authoritative origin (`runner`, `launcher`, `launch_failure`, `cancel`); all
other terminal-to-terminal writes are rejected. Terminal statuses are
`succeeded`, `failed`, `cancelled`, and `timed_out`. The reaper will not
overwrite a spawn that the runner already terminated with a higher-authority
origin.

### Reconciliation Behavior

`reaper.py:reconcile_spawns()` is a read-only batch projection used by list, stats,
reference-discovery, and dashboard callers. For each active row it calls
`peek_reconciled_active_spawn()`, which may return an in-memory terminal view but
does not persist state, terminate process scopes, or check process depth.

`reconcile_active_spawn()` is the mutating repair operation used by doctor orphan-run
repair. It fails closed unless `is_root_side_effect_process()` is true and applies the
same reconciliation decision rules. Under the spawn lock it first snapshots exact
cleanup targets into `reaper_cleanup_claim.json`, then persists the terminal result,
then terminates the claimed scopes. This finalize-first order makes state convergence
independent of slow cleanup; a crash leaves a durable claim for the next doctor pass.

Shared liveness decisions skip recent activity and live runner PIDs; prefer durable
report completion, cancel precedence, and recorded runner terminal tuples; and
otherwise classify stale active rows as orphan failures after startup/finalization
grace periods. PID checks use the recorded creation time to guard against reuse.

`spawn_report_has_durable_completion(runtime_root, spawn_id)` reads `report.md` and returns True
for non-empty report content that is not a terminal control frame (`cancelled`/`error`
JSON) and is not a `# Spawn failed` generated markdown wrapper. Used by both reaper
and cancel convergence paths.

`reconciliation.py:completion_or_cancel_decision()` centralizes
durable-completion-vs-cancel precedence for reconciliation callers: if a durable
report exists, the spawn resolves `succeeded` regardless of cancel intent; otherwise
pending cancel intent resolves `cancelled` with the intent's exit code and error.
Call this helper rather than branching on completion and cancel independently; a
late cancel must not downgrade a completed spawn.

**Managed-primary orphan cleanup:**

For managed primaries, the claim combines recorded spawn-owned scopes with fallback
targets derived from the managed-primary snapshot when available. Birth-validated
scope cleanup consumes only that claim after terminal finalization. Successful targets
gain durable release markers; failed targets remain claimed for retry. A runner-origin
terminal write clears a reconciler claim without signalling because runner authority
supersedes reconciler cleanup intent.

## Patterns

### Platform Locking

Use `platform.locking.lock_file(path)` for all cross-process locking:
- POSIX: `fcntl.flock(LOCK_EX)` — advisory, kernel-backed
- Legacy native-Windows branch (untested): `msvcrt.locking()` with retry loop
  (50 ms sleep)

Thread-local reentrancy: a thread that already holds the lock can re-enter on the
same path without deadlocking. Do not use `threading.Lock` or `fcntl` directly —
the platform module handles both OS and thread-reentrancy.

Per-spawn lock paths stay stable while their spawn exists. `lock_gc.py` may unlink an
orphan only under a fresh, non-reentrant validated exclusive acquisition, and cleaned
session locks use the same unlink-before-release primitive. No other release or delete
path may unlink coordination locks. In particular, unlinking while a reentrant outer
frame remains locked strands that frame on an orphaned inode and permits split-brain on
a recreated path.

`work_scope.py` resolves each work directory. `work_state.py` owns the work-item
models, metadata codec, and shared directory-location primitives. `work_store.py`
provides pure read projections; `work_repository.py` serializes all status and
directory-namespace mutations behind the stable project-level `work-store.flock`.
Mutable metadata lives in
`<context.work root>/<slug>/__status.json`; there is no separate rename-intent file.

### WorktreeMetadata

`WorktreeMetadata` in `work_state.py` preserves path, branch, repository, name,
pending, and managed fields in work-item status. Current code reads the path and
pending flag for dashboard display but does not provision, rename, or clean up git
worktrees. Archiving or reopening a work item clears `pending`.

**Path separator normalization**: `WorktreeMetadata.path` and `.repo_path` normalize
backslash separators to POSIX (forward slash) at the Pydantic validation boundary via
`@field_validator(..., mode="before")`. This keeps stored metadata separator-stable.

### User-Level Storage for New Features

New features that need user-level storage (git clones, cache, custom data) go under
`get_user_home()` from `user_paths.py`. Do not hardcode `~/.meridian/` or introduce
new OS-specific home-directory branches.

### Anti-Patterns

**Don't read `state.json` without `read_state()`** — raw JSON reads bypass Pydantic
validation and miss the `SpawnRecord` reconstruction from `starting-prompt.md`.

**Don't use `*_for_write()` on read paths** — it mints or materializes identity,
triggering setup side effects in CI and bare directories.

**Don't use `open()` for state file writes** — use `atomic_write_text()` or
`append_text_line()`. Plain writes don't survive crashes.

**Don't acquire `spawns_flock` for per-spawn mutations** — the global lock serializes
spawn ID allocation, initial row publication, and abandoned-stage GC. Acquiring it for
later individual mutations creates unnecessary contention. Use `write_state_locked()`
(per-spawn `locks/spawns/<id>.lock`) for external writes.

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

## Reopening a spawn

`reserve_run` takes mutation(shared) → spawns allocation lock, repeats the
read-only reaper busy projection, and atomically publishes max surviving K + 1.
Its runner PID/birth stamp protects queued startup; no generation is copied.
The chat lease is acquired only after releasing the allocation lock. A live
owner refuses; transient lock contention without a live owner retries for 10 s.
Session scope binds the acquired generation to the new row. Never reopen a
terminal row in place or mint run 1 when all loose runs have been archived.

The continue target is the latest generation-bound run's current/entry chat;
failed unbound reservations cannot redirect it. The newest linked generation
owns a chat; only chats without any linked generation fall back to the run
publishing `current_chat_id`. Session `spawn_id` remains an exact run key.
