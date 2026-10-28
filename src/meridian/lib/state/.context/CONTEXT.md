# lib/state/ — Context

## Architecture

### Dual-Root Layout

State splits across distinct roots; know which goes where before writing anything:

```
meridian.toml
  [project] id                      — committed machine-managed project identity

bare path
  sha256(canonical path)            — ad-hoc identity; no file is written there

~/.meridian/projects/.locks/<id>.lock
                                    — shared session-lifetime / exclusive deletion gate

~/.meridian/projects/<id>/          ← user runtime, never committed
  origin.json                       — ad-hoc path attribution and activity only
  sessions.jsonl                    — session events (append-only; absent v means v1,
                                      unsupported versions warn and are skipped by
                                      reads; read-then-append decisions refuse them
                                      in scope)
  sessions/<chat>/work              — stable symlink to the chat's work directory: the
                                      item's `work-items/<id>/work` link, else the
                                      execution's ambient directory; a projection of
                                      the session row, never authority
  sessions/<chat>/task              — stable symlink to the chat's source checkout: the
                                      execution's pinned checkout, else the item's
                                      `work-items/<id>/task` link, else the project
                                      root (`launch/session_links.py` publishes both
                                      for every live execution, primary or spawn)
  work-items/<id>/work              — the item's directory, in `work/` or the archive
  work-items/<id>/task              — the item's `task_dir` while it is a directory,
                                      else the project root, whether the item is active
                                      or done (`work_store.item_checkout()`, the one
                                      rule); both written only by the work store,
                                      inside its lock, and rebuildable
  locks/spawns/<spawn_id>.lock      — stable per-spawn external-writer lock
  locks/process-scopes/<spawn_id>.lock
                                    — stable process-scope sidecar mutation lock
  locks/reaper-cleanup/<spawn_id>.lock
                                    — stable reaper cleanup lock
  locks/gc.lock                     — lock-GC pass serialization
  spawns/.staging/<unique>/         — unpublished builds and verified-archive retirement buffers
  spawns/<spawn_id>/
    state.json                      — authoritative spawn state (schema v3)
    launch.lock                     — runner-lifetime flock, acquired before publication
    history.jsonl                   — legacy runner event stream (no longer written), never read
    attempt-N/                      — legacy retry evidence; new runs do not create it
    runner-lifecycle.jsonl          — runner breadcrumb journal (signals, phases, atexit)
    process_scopes.json             — durable process identities + release markers
    reaper_cleanup_claim.json       — pending finalize-first cleanup targets
    permission_requests.jsonl       — requests the model made that nobody could approve
                                      (`permission_journal.py`: one format for broker and
                                      finalization writers; `spawn show` reads it)
    report.md · stderr.log · params.json · tokens.json
    observed-spawns.json            — this parent's consumed child IDs and explicit-wait
                                      leases; shared by Pi and Claude delivery; goes with
                                      the spawn, so pruning one forgets its consumption
  pi-bash/<spawn_id>/observed-spawns.json
                                    — legacy-observed-spawns (#719): read, never deleted,
                                      and mirrored by record() when this Pi dir exists
<context.work root>/<slug>/         ← context-resolved, NOT repo-local
  __status.json                     — mutable per-work-item metadata; readers accept
                                      absent v as v1, writers do not emit v yet
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

Sessions remain event-sourced JSONL (`sessions.jsonl`). Lifecycle and control
reads use the keyed seam in `session_store.py`; multi-chat callers select all
needed chats in one pass and project the accepted binding and generation.
Discovery uses the disposable history index; its recency reads
select the highest-ordinal generation from the same authority.
An absent event `v` is legacy v1. Events with an unsupported version are
skipped with a warning rather than being folded as if their shape were current.
Lock-scoped reads that decide what to append refuse unsupported rows in their chat
or native-session scope with `UnsupportedSessionFormat`; blind appends never read
the journal. Paths that run on every command (the one-time import) scope reads to
the chats they write, so a newer build's rows elsewhere cannot disable an older
build's commands. Full-snapshot repairs refuse; background runs log that at debug.

A scoped `SessionBindings` snapshot raises `ChatOutsideBindingScope` for chats
outside its scope; cross-chat repair uniqueness requires `scope=None`.
See [history.md](history.md) for read ordering, append epochs, projection and
namespace contracts.

### One-time native identity import

Runtime authority resolution invokes `ops/legacy_native_import.py` once for an
existing sessions journal. Its atomic marker records unresolved chats; ordinary
reads never repair missing keys afterward. Import binds through the same
`session_binding.py` lock-scoped path as single-session updates. One replay per
batch avoids quadratic work; the import lock precedes the history-mutation gate
and sessions lock. The locked recheck reads only the chats being bound; an
unsupported row in one of them defers the import without a marker, like an
unavailable source. Historical restored records stay inert.

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

### JSON Sidecar Contract (`json_doc.py`)

Small JSON sidecars (session leases, primary metadata, process scopes, cleanup
claims, spawn scopes, idle state, Pi lifecycle, autosync state and conflicts) are
Pydantic models persisted only through `read_model()` / `write_model()` /
`mutate_model()`. `read_model()` returns `None` for a missing, truncated or invalid
file, so a corrupt sidecar loads as absent and the next write repairs it.
`mutate_model()` requires the caller's lock and re-reads under it; there is no
unlocked read-modify-write. A new sidecar adopts this seam rather than hand-rolling
read, validate, lock and replace.
Private coordination files (Pi's private state, spawn observations) and their
nested records subclass `VersionedPrivateDoc`: strict, frozen, extra keys ignored,
and a `v` field must be the integer 1, because a strict `Literal[1]` alone accepts
`true` and `1.0`.
Idle state adds one diagnostic to that generic absent-on-invalid contract: if
the document's `v` names another format, `idle_store.py` warns with the path and
that a different Meridian build wrote it.

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

### Monotonic ID Generation

**Spawn IDs** (`spawn-id-counter`): incremented under `spawns_flock` at reservation
time. IDs can be reserved before the spawn row exists (`reserve_spawn_id()`).
Format: `p1`, `p2`, `p3`, …

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

List, stats, reference-discovery, and dashboard callers call
`spawn_status.overlay_derived()` directly; single-row readers call `read_derived()`.
Both may return an in-memory terminal view, but never persist state, terminate process
scopes, or check process depth.

`reconcile_active_spawn()` is the mutating repair operation used by doctor orphan-run
repair. It fails closed unless `is_root_side_effect_process()` is true and delegates to
`SpawnApplicationService.finalize_dead_launch()`, the only dead-launch outcome writer.
That method writes only after `claim_dead_launch()` returns `Claimed`, retaining the
free runner-lifetime `launch.lock`, and persists the same status, exit code, and error
that `read_derived()` projects: cancel intent yields `cancelled`, otherwise runner
disappearance yields `failed`/`crashed`. Origin and timestamps may differ. It never
finalizes a lockless legacy row.

The background repair on primary launch is `recover_crashed_launches()`, not a row walk:
one pass at a time under `locks/repair.lock`, candidates are the history index's active
rows in spawn-ID order resuming after `repair-cursor`, at most 64 launch-lock attempts
and 32 recoveries per pass, lockless rows untouched, and a busy index skips the pass.
Only doctor still reads every `state.json` (`iter_repair_candidates`), and only doctor
resolves a terminal row's leftover reaper claim.
Before finalization it snapshots exact cleanup targets into
`reaper_cleanup_claim.json`; after finalization it terminates the claimed scopes. This
finalize-first order makes state convergence independent of slow cleanup; a crash
leaves a durable claim for the next doctor pass.

Cancel convergence uses the same launch-lock claim for lock-backed rows. A report or
runner-exit breadcrumb is not an outcome: when the runner is gone, cancel intent still
yields `cancelled`. Lockless legacy rows retain the old report/cancel precedence until
their explicit-command compatibility path is removed.

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

`work_scope.py` resolves each work directory. `work_state.py` owns the work-item
models, metadata codec, and shared directory-location primitives. `work_store.py`
provides pure read projections; `work_repository.py` serializes all status and
directory-namespace mutations behind the stable project-level `work-store.flock`.
Mutable metadata lives in
`<context.work root>/<slug>/__status.json`; there is no separate rename-intent file.
Its reader treats absent `v` as v1 and preserves unknown fields with a warning;
writers deliberately omit `v` until the next format change. A newer version gets
a separate compatibility marker displayed as `requires-newer-meridian`, while the
single mutation seam validates every active and archived copy before touching disk.

### Work Item Directory Moves

The location is the state. `work done` moves `<work root>/<slug>` to
`<archive>/<slug>` and `work reopen` moves it back, each in one step; there is no
"done but not yet moved" status and no sweep to finish one. A directory in the archive
is done by location, whatever its `__status.json` still says, so a crash between the
rename and the metadata write reads correctly and `meridian doctor` normalizes the
file. Reopen writes the reopened status before its move, so an interrupted reopen
leaves the item archived and running the command again repairs it. The one state the
location cannot explain — an active directory whose status says `done` — is repaired
in place by `work reopen` (and so by `work switch`), instead of refusing as already
active forever.

A running spawn is not a reason to defer: its work directory is a per-chat link
(`launch/session_links.py`) that points at a link the work store owns,
`work-items/<id>/work`, so the directory moves under it and the next write lands at the
new location. `write_state_locked` republishes `work-items/<id>/{work,task}` before
and after every mutation of the item — before, to repair whatever a crash or an older
build left, which is why re-running a command that then refuses ("already archived")
still repairs the links. No session row is written by `done`, `reopen` or `task-dir`:
the chat link depends on the chat's row and the execution's scope, never on where the
work store keeps the item. A rename repoints the old name's links at the new item so
live chat links keep resolving until the sessions have followed, then the old name's
links are dropped (`ensure_item_links`). That forwarded `work` link is the only trace a
rename that stopped after the move leaves, so it is also the evidence a repeated
`work rename OLD NEW` needs to finish the rename (`rename_stopped_after_move()`): with
no such link the old name is just not found. A done item keeps its checkout, because
its agents are still working there: `work_store.item_checkout()` is the one rule behind
the item's `task` link, and `meridian task-dir` reads the same item, active or done.
`meridian doctor` republishes every item's links and drops the ones whose item is gone
(`sweep_item_links`, reported as `item_links`), except a gone name whose `work` link
still resolves — a stopped rename's forward link, which its re-run still needs.
One window is not covered: a kill between the directory move and the forward-link
publish leaves no forward link, so the re-run reports the old name as not found. Doctor
then drops the old name's links, and `work rename` must be re-run from the new name.
The window is microseconds; closing it would mean publishing the forward link before
the move.

`move_work_dir` refuses when both locations exist, naming both, and never deletes
either copy. Only `work delete --force` removes a directory.

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

### Atomic writes

`atomic_write_text()` writes a same-directory temp file at runtime-state mode `0600`, fsync, then `os.replace()`; user-owned project files and work-item metadata use the preserve-mode writer in `lib/platform/atomic.py`. `atomic_publish_dir()` renames a complete same-volume stage to a nonexistent destination and fsyncs the parent. `append_text_line()` is binary so JSONL offsets stay stable. Enforced by `tests/contract/test_state_write_conformance.py`.

## Related KB

> KB lives at `$MERIDIAN_CONTEXT_KB_DIR` (see `meridian context kb`).

- `$MERIDIAN_CONTEXT_KB_DIR/architecture/state-system.md` — full dual-root layout,
  per-spawn state, session state, work item storage, and read vs write resolution
- `$MERIDIAN_CONTEXT_KB_DIR/architecture/spawn-finalization.md` — terminal write
  authority lattice, how finalization interacts with the reaper
## Reference details

### Work-item metadata and project identity

`project_identity.py` is the only identity reader and writer. Its named minting
seam atomically writes init scaffolds with their committed identity. A root
containing `meridian.toml` or `mars.toml` has committed identity; a marker-less
root gets a stable `adhoc.<slug>.<hash>` ID derived from its canonical path. Ad-hoc
state is materialized only on writes, under `~/.meridian/projects/<id>/`, and
nothing is written into the source directory. Committed projects can move without
losing runtime history; moving an ad-hoc directory changes its identity. Project
lifetime gates sit outside the deletable project root and are never unlinked.
Real-launch materialization pre-creates the exact lock inode;
launch bind only projects that path and remains write-free.

Work items live under the `[context.work]` root (default
`{user_home}/context/<id>/work/<slug>/`), resolved by `work_scope.py` /
`work_state.py` — **never** the project repo. See `docs/configuration.md` for
context-path resolution.

Work metadata reads tolerate, preserve, and warn about unknown fields. A newer `v`
gets a compatibility marker displayed as `requires-newer-meridian`; it never
collides with the stored status, and mutations validate every located copy before
touching disk. Malformed metadata still projects directory defaults, but always
warns with the file path. Repeated reads warn once per file per command.


### Spawn mutation seam

Every update to a published spawn calls `write_state_locked()`. It acquires
the shared history mutation gate, then `locks/spawns/<id>.lock`, re-reads current state, applies a pure mutator, and writes
atomically. The lock identity is outside the artifact directory it protects and remains
stable while the spawn exists. Orphaned identities are unlinked only through lock GC's
validated-exclusive, unlink-before-release seam.

Process-scope registration and release markers route through the locked sidecar
mutation seam. When both locks are needed, acquire the spawn-state lock before the
scope-sidecar lock. Registration is refused after the spawn becomes terminal or a
cleanup claim exists, so a process scope cannot appear after cleanup targets are fixed.
Any writer into a destructible `spawns/<id>/` directory that can outlive deletion must
not recreate its owner. Parent-creating mutations take the spawn-state lock and refuse
a missing published row through `published_artifact_lock()`; callback-only writers use
`mutate_published_spawn_artifact()` on the same seam. Signals, scope, history,
reaper diagnostics, and harness journals all use it.
The spawn repository and process-scope projection are persistence leaves with a
one-way dependency: the projection may use repository reads and lock paths, while
the repository never imports the projection. Cross-leaf operations belong in
`spawn_aggregate.py`; in particular, published-spawn deletion owns the lock order
above. Reaper claims consume one immutable projection snapshot containing
both scopes and released IDs, read under a single projection-lock acquisition.


### Launch ownership

`launch_lock.py` owns the non-reentrant runner-lifetime flock. It is separate
from mutation locks: the background worker inherits the same open-file
description via `dup`/`pass_fds`, then immediately restores close-on-exec and
records its own identity. Release closes descriptors; never use `LOCK_UN`,
which would unlock the worker too. Harnesses never inherit this descriptor.
`LaunchSlot` owns creation before ID allocation; `bind_staged()` returns a frozen
`LaunchHandle` capability with a concrete ID and fd. Lifecycle/service creation
requires a slot; only low-level `start_spawn` permits lockless test seeding.

Explicit continue resolves the named spawn's current chat owner, takes that
owner's lock before the named source lock, and sweeps their birth-verified scopes
before creating another row. An exact-continue parent publishes the new row as
the chat owner while its launch lock is still held; a background worker confirms
that pointer after hand-off. Lockless legacy rows alone use
`legacy_runner_alive()`'s 15-second pre-worker PID-less window and convert under
the mutation lock. Lock-backed rows have no grace: a free lock derives terminal.
This does not change v3 status authority.


### History discovery and retention


Machine-wide retention has disjoint owners: committed-root scanning rejects
`adhoc.*`, while ad-hoc scanning accepts only that prefix. Ad-hoc activity is the
latest tree mtime under `projects/<id>/` or `context/<id>/`, including
`origin.json`, plus persisted spawn/session start and finish times. Pruning
rechecks eligibility under the exclusive project-lifetime gate and removes
`projects/<id>/` with `context/<id>/`.
`state.adhoc_retention_days` is user-config/environment-only; cwd project config
must never control a machine-wide scan.

Use `history_index.py` for filtered discovery, not repeated broad authority scans.
Keep lifecycle/control decisions on direct authoritative reads. All new indexed
writers must publish dirty intent before file mutation; SQLite is never a writer
dependency. Restored `record_mode="historical"` aggregates are inert and immutable;
their reserved `historical-` generation prefix makes the append guard O(1).
Per-operation session reads and all appends use the keyed seam in `session_store.py`;
multi-chat reads use one `chats_rows` pass, never a loop over single-chat reads.
Indexed queries build a missing projection, then make one catch-up attempt bounded
by the caller deadline (two seconds by default). Contention raises
`HistoryIndexBusy`; incomplete coverage is never a negative result. Every caller
gives an absent projection a separate, sticky 15-second cold build before its
ordinary read attempt. An exhausted cold build latches failure.
Warm catch-up projects `sessions.jsonl` to the committed end even when its writer
lock is busy; the lock gates marker acknowledgement, not the read. Spawn and archive
catalog projection still require their source locks. Incremental session catch-up
loads only the working sets for chats touched after its cursor.
The index file and its sidecars (marker queue, GENERATION, catch-up/database/marker
locks, init latch) are named by both `SCHEMA_VERSION` and the latest supported
session event version; either change builds a new file from authority and never
touches an older build's file. Unsupported session rows are skipped; the cursor
stores their versions so every indexed read warns.
Never migrate in place.
See [history.md](history.md) before changing locks, projection,
transcript retry behavior, ZIP retention, or restore.


### Reconciliation: repair and cleanup


Readers get status only from `spawn_status.py`: `read_derived()` for one spawn and
`overlay_derived()` for rows from a scan or the index. For lock-backed rows, they hold
the shared launch lock across the authority read. Lockless legacy rows use the bounded
legacy liveness rule instead. Both may return an in-memory terminal status, never
persist state or terminate processes, and are safe at any depth.

Use derived status for liveness, display, wait, and idle questions. Selection for
cancel/archive/destruction uses stored status until dead-launch finalization persists
an outcome. `claim_dead_launch()` returns `Claimed`, `Settled`, `Live`, or `Lockless`;
only `Claimed` authorizes `SpawnApplicationService.finalize_dead_launch()`, the sole
writer of a dead lock-backed launch's outcome.

`reconcile_active_spawn()` is the side-effectful repair path. It runs from doctor
background repair and fails closed outside root depth. It delegates persistence to
`finalize_dead_launch()`, which retains the claimed runner-lifetime `launch.lock`;
lockless legacy rows are never finalized here. The repairer snapshots a cleanup claim,
persists the derived outcome while retaining the launch lock, then terminates the
claimed, birth-validated scopes. A separate stable
cleanup lock prevents concurrent repairers from double-signalling; terminal rows
retain failed claims for the next doctor pass. Startup candidate discovery streams
authoritative spawn rows directly, selecting active rows and rows with a cleanup
claim; it does not trust the disposable history index or retain a corpus-sized scan.
Orphan primaries are captured only after the whole pass: the capture guard defers
while any row bound to the same native session is active, and crashed `--continue`
generations share one. Primary scope termination additionally requires positive
runner-death evidence against the recorded birth, with the same one-second clock
tolerance as the liveness check. Unknown identity and access errors never authorize
primary scope termination, including replayed terminal claims; they do not block
status finalization.
Read-only projected status is not authorization to terminate a scope.


### Native chat identity


A chat keeps one `(harness, native_store, harness_session_id)` binding across
starts and updates. `update_session_harness_id` atomically returns
`BindOutcome` (`Bound`/`Same`/`Conflict`) from the one pure rule in
`native_binding.bind`; callers mirror its accepted ID, not their candidate.
`session_fold.py` owns event models and replay; `by_native_key(records)`
inverts accepted chat records without discarding aliases or doing I/O. A later
accepted start projects every earlier unstopped live generation of that chat as
stopped at the new start time: ordinary starts hold the chat's exclusive lifetime
lock, while observed-chat starts mint a new chat and immediately stop it.
Historical multi-ID arrays are ignored on read. Chat IDs are `c<digits>` by shape
(`core/refs.is_chat_id`): `reserve_chat_id` mints them and `start_session` rejects a
caller-supplied ID of any other shape. `native_generations` is the one native-ID lookup: it
finds a native ID on any generation, picks the newest with a native session key (else the newest), and refuses an ID recorded under two harnesses or two
native stores.

Fork and context-source ancestry belong to the chat, not its latest generation:
accepted starts carry both parent identities and original creation time forward.
Tmux targets belong to the generation and may be absent. HistoryIndex projects
parent edges and native model observations from the journal; list consumers batch
model lookups rather than scanning observations once per row.


### Chat→spawn link

A chat runs as one or more generations, one start event each: a launch, a
`--continue`, or the new chat a running primary moves into after `/clear`. Every start
event names the spawn that runs that generation (`spawn_id`) when it is written. `session_identity.chat_owner_spawn` relies on it: it returns the newest
generation's spawn that has a row, with no scan and no fallback, so a writer that links
late or not at all leaves spawn commands unable to name the chat's owner ("Chat cN has no
spawn"); resume, read and `--from` still work on the chat.

- Children write the spawn row first, then start the session.
- The primary runner reserves its ID (`spawn_store.reserve_spawn_id`) before
  `session_scope`, so the start event can carry it, and passes the same ID to
  `SpawnReservation`. Its
  post-start `update_session_spawn_id` stays: it is the only writer of the session's
  `history_id`, which the session listing needs to hide archived primaries.
- An observed (`/clear`) chat's start event names the observing run.
- An exact continue writes no link until its session starts: the start event is the only
  writer of a chat's owner. Until then `spawn show`, `wait`, `cancel` and
  `message` by chat ID name the previous run (a short window; once the row exists but its
  session has not started they can act on a finished run), and a racing continue is
  refused by the chat lock (`ChatBusyError`) in its worker.
- A spawn row's `chat_id` is its own chat: an exact continue reserves it with the
  continue target, so a continue refused before its session starts still resumes that
  chat. A fresh child has no chat until it starts and carries its caller's chat as a
  placeholder (`owner_chat_id` is the caller's chat); a child that fails before its
  session starts therefore resumes or reads the caller's conversation. Reserving it
  chat-less would need every reader that groups children by `chat_id` (`spawn list
  --chat`, work dashboard and sessions, `session_records_for_spawns` full scans) to
  switch to `owner_chat_id`; not done.

A crash between reservation and row write leaves a link to no row; readers skip it.
`history_id` is the relocation-stable ID of a spawn's history. Only the generation a
spawn entered with shares it; a `/clear` chat keeps its own. A reader matching
generations to a spawn therefore accepts a generation only when its `spawn_id` matches
and its `history_id` is absent or equal to the spawn's.
Rationale and rejected reader-side fallbacks: KB `decisions/chat-spawn-ownership.md`.


### Module map


- `user_paths.py` — `get_user_home()`. Start here for any new user-level storage.
- `paths.py` — `RuntimePaths`, read vs write root resolvers.
- `spawn_store.py` — listing/query, creation, and mutation interface; scans return `SpawnScan`.
- `spawn_aggregate.py` — published-row deletion and spawn-owned artifact lifetime guard.
- `work_state.py` / `work_store.py` / `work_repository.py` — work-item models and
  codec, pure reads, and the single locked mutation repository, respectively.
  `work_repository.move_work_dir()` is the one active↔archive move: it refuses when
  both copies exist and never deletes one. The location, not a status field, says
  whether an item is active, so `work done` moves at once. `write_state_locked`
  republishes the item's `work-items/<id>/{work,task}` links around every mutation, so
  nothing else repoints anything when an item moves.
- `session_store.py` — Session event I/O and lease ownership.
  `session_writes()` is the one session write epoch and `set_work_attachments()` the
  one batched attachment write.
- `json_doc.py` — tolerant Pydantic reads plus locked atomic mutation for JSON sidecars,
  and the `VersionedPrivateDoc` base for private coordination files.
- `idle_store.py` — user-scoped idle stretch state, per-file locking, and seven-day lazy GC;
  a version mismatch remains absent behaviorally but warns that another build wrote the file.
- `session_fold.py` — Public event models, pure replay, and accepted-key inversion.
- `atomic.py` — atomic write primitives. All state writes use these.
- `jsonl.py` — the shared newline-commit, tail-repair, and durable-append policy.
- `spawn_status.py` — lock-consistent status for every reader and
  `claim_dead_launch()`'s typed writer-side claim.
- `spawn_observations.py` — consumption records and birth-verified explicit-wait
  leases across primary generations. Writes go through the parent's published-spawn
  lock and never recreate a deleted spawn directory; readers union the current file
  with the legacy `pi-bash/` one. Normal wait/ack writers block for that lock;
  best-effort shown-result observations wait up to one second and preserve their
  output when the lock or filesystem is unavailable.
- `spawn_ownership.py` — the one rule for which parents a delivery caller owns: a live
  primary owns every primary generation of its chat, anyone else owns only itself. Also
  owns `is_owed_child` (a child is owed only when `launch_mode == "background"`), which
  the Pi spawn-watch extension mirrors as `isBackgroundLaunch`. `spawn collect`, the Pi
  launch's spawn-watch scope, and the Pi drain all read it here.
- `reaper.py` — root-only repair that claims dead launches, persists their projected
  outcome through the application service, then cleans claimed process scopes.

## Related .context/

- [../../harness/.context/CONTEXT.md](../../harness/.context/CONTEXT.md) — `SpawnExtractor` contract and harness-owned native evidence
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — launch pipeline that writes spawn state via `SpawnStore`; composition seam, prepare/bind split
- [../../platform/.context/CONTEXT.md](../../platform/.context/CONTEXT.md) — `lock_file()`
  implementation details, Windows/POSIX branching
