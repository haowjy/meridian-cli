# lib/state/ — State Layer

File-backed authority for all Meridian runtime state. No service or in-memory
object survives process death. SQLite is permitted only as a rebuildable projection;
authoritative state must remain independently recoverable from its source files.

## Roots

State splits across distinct roots — understand which goes where before writing anything:

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
    report.md · stderr.log · params.json · tokens.json
<context.work root>/<slug>/         ← context-resolved, NOT repo-local
  __status.json                     — mutable per-work-item metadata; readers accept
                                      absent v as v1, writers do not emit v yet
  prompts/ handoffs/ …              — work artifacts
```

`project_identity.py` is the only identity reader and writer. Its named minting
seam atomically writes init scaffolds with their committed identity. A root
containing `meridian.toml` or `mars.toml` has committed identity; a marker-less
root gets a stable `adhoc.<slug>.<hash>` ID derived from its canonical path. Ad-hoc
state is materialized only on writes, under `~/.meridian/projects/<id>/`, and
nothing is written into the source directory. Committed projects can move without
losing runtime history; moving an ad-hoc directory changes its identity. Project
lifetime gates sit outside the deletable project root and are never unlinked.

Work items live under the `[context.work]` root (default
`{user_home}/context/<id>/work/<slug>/`), resolved by `work_scope.py` /
`work_state.py` — **never** the project repo. See `docs/configuration.md` for
context-path resolution.

Work metadata reads tolerate, preserve, and warn about unknown fields. A newer `v`
gets a compatibility marker displayed as `requires-newer-meridian`; it never
collides with the stored status, and mutations validate every located copy before
touching disk. Malformed metadata still projects directory defaults, but always
warns with the file path. Repeated reads warn once per file per command.

## Spawn State: Per-Spawn Files

Spawn state lives in individual `state.json` files (`spawns/<id>/state.json`),
not a global event log, so status reads stay O(1) instead of replaying O(n) events.
The layout originated in the v2 migration; published rows now use schema v3.

## Launch ownership

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

## Spawn Mutation Seam

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
reaper diagnostics, failure sentinels, and harness journals all use it.
The spawn repository and process-scope projection are persistence leaves with a
one-way dependency: the projection may use repository reads and lock paths, while
the repository never imports the projection. Cross-leaf operations belong in
`spawn_aggregate.py`; in particular, published-spawn deletion owns the lock order
above. Reaper claims consume one immutable projection snapshot containing
both scopes and released IDs, read under a single projection-lock acquisition.

## History discovery and retention

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
See [.context/history.md](.context/history.md) before changing locks, projection,
transcript retry behavior, ZIP retention, or restore.

## Write-Boundary Path Normalization

`normalize_path_for_write()` in `paths.py` normalizes path strings to stable absolute
form (`os.path.abspath(os.path.normpath(...))`) at the write boundary. Applied to
`control_root`, `task_cwd`, and `execution_cwd` in both `spawn_store` and
`session_store`. Other path-like fields (`claude_config_dir`, `agent_path`,
`skill_paths`) are not yet normalized; see `.context/FUTURE` for the scope-out.
New persisted path fields should use this helper at their write site.

## Atomic Write Contract

State-facing writes go through `atomic.py`, which delegates file replacement to the
dependency-neutral `lib/platform/atomic.py`. Never write state files with plain `open()`.

- `atomic_write_text()` / `atomic_write_bytes()` — write to same-directory temp,
  force runtime-state mode `0600`, `os.fsync()`, then `os.replace()` (atomic rename).
  Either the old or new file exists — never a partial write. User-owned project files
  and context work-item metadata use the preserve-mode platform atomic writer instead.
- `atomic_publish_dir()` — rename a complete same-volume stage into a destination
  that must not exist, then fsync the publication parent.
- `append_text_line()` — binary mode so JSONL newline encoding and byte offsets
  remain stable.

JSONL reads, tail repair, and durable appends use `jsonl.py`: only newline-terminated
rows are committed, empty rows are ignored, and malformed complete rows are skipped
with debug diagnostics by default. Callers such as the archive catalog that must fail
closed select the strict malformed-row policy explicitly. Repair is read-then-replace,
so callers hold their writer lock or own a single-writer file.

A crash in the middle of a plain `open()` write leaves a partial file; partial
`state.json` fails Pydantic validation on next read.

## Read vs Write Root Resolvers

Use the correct resolver — they have different side effects:

| Resolver | Materializes identity? | Use when |
|---|---|---|
| `resolve_project_runtime_root()` | No | Read paths (list, show, status) |
| `resolve_project_runtime_root_for_write()` | Yes (under lock) | Write paths only |

Using `*_for_write()` on a read path creates user-home state (and mints a committed
ID when a marker exists), triggering setup side effects in CI.

Transcript reads go through `ops/session_target.resolve_transcript_source`: ref →
chat → accepted native key → exact harness reader. Artifact storage never redirects
to spawn transcripts; unbound or missing native sources are unavailable. Restored
historical records and imported archive refs read a sealed `snapshot` source instead.

## Reconciliation Behavior

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

## Native Chat Identity

A chat keeps one `(harness, native_store, harness_session_id)` binding across
starts and updates. `update_session_harness_id` atomically returns
`BindOutcome` (`Bound`/`Same`/`Conflict`) from the one pure rule in
`native_binding.bind`; callers mirror its accepted ID, not their candidate.
`session_fold.py` owns event models and replay; `by_native_key(records)`
inverts accepted chat records without discarding aliases or doing I/O. A later
accepted start projects every earlier unstopped live generation of that chat as
stopped at the new start time: ordinary starts hold the chat's exclusive lifetime
lock, while observed-chat starts mint a new chat and immediately stop it.
Historical multi-ID arrays are ignored on read.

## Entry Points

- `user_paths.py` — `get_user_home()`. Start here for any new user-level storage.
- `paths.py` — `RuntimePaths`, read vs write root resolvers.
- `spawn_store.py` — listing/query, creation, and mutation interface; scans return `SpawnScan`.
- `spawn_aggregate.py` — published-row deletion and spawn-owned artifact lifetime guard.
- `work_state.py` / `work_store.py` / `work_repository.py` — work-item models and
  codec, pure reads, and the single locked mutation repository, respectively.
- `session_store.py` — Session event I/O, lease ownership, and the canonical
  `session_exists()` predicate (a chat record exists; a harness ID is not required).
- `json_doc.py` — tolerant Pydantic reads plus locked atomic mutation for JSON sidecars.
- `idle_store.py` — user-scoped idle stretch state, per-file locking, and seven-day lazy GC;
  a version mismatch remains absent behaviorally but warns that another build wrote the file.
- `session_fold.py` — Public event models, pure replay, and accepted-key inversion.
- `atomic.py` — atomic write primitives. All state writes use these.
- `jsonl.py` — the shared newline-commit, tail-repair, and durable-append policy.
- `spawn_status.py` — lock-consistent status for every reader and
  `claim_dead_launch()`'s typed writer-side claim.
- `reaper.py` — root-only repair that claims dead launches, persists their projected
  outcome through the application service, then cleans claimed process scopes.

## Spawn Subpackage

`spawn/` contains domain models, strict v3 persistence helpers, the one-shot legacy
v2 read upgrader, and pure transitions.
→ [spawn/AGENTS.md](spawn/AGENTS.md)

## Anti-Patterns

**Don't read `state.json` without `read_state()`** — raw JSON reads bypass Pydantic
validation and miss `SpawnRecord` reconstruction from `starting-prompt.md`.

**Don't use `*_for_write()` on read paths** — mints committed identity or
materializes ad-hoc user state during an otherwise pure read.

**Don't write state files with `open()`** — use `atomic_write_text()` or `append_text_line()`.

**Don't unlink coordination locks outside the GC seam** — `lock_gc.py` and cleaned
session handling may call `unlink_validated_lock()` only under a fresh, non-reentrant,
validated exclusive acquisition, immediately before release. Never unlink at ordinary
release time or inside a reentrant context: leaving any lock held on the orphaned inode
lets another contender validate against a recreated inode and causes split-brain.

**Don't acquire `spawns_flock` for per-spawn mutations** — the global lock serializes
spawn ID allocation, initial row publication, and abandoned-stage GC. Later mutations
use `write_state_locked()` (`locks/spawns/<id>.lock`, stable until orphan lock GC).
Published-row deletion uses `delete_published_spawn()` under that same stable lock;
when deletion also takes the process-scope projection lock, the order is spawn lock
then projection lock. This composition belongs in `spawn_aggregate.py`, not either leaf
repository. Pruning acquires the shared history mutation gate before `spawns_flock`. Pending reaper cleanup
claims block deletion so durable cleanup intent is never discarded.

**Don't hardcode `~/.meridian/`** — use `get_user_home()` from `user_paths.py`.
It honors `MERIDIAN_HOME` and centralizes home resolution.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — full dual-root layout, per-spawn state,
   monotonic ID generation, terminal write authority lattice, and worktree metadata.

## Related

- `../harness/AGENTS.md` — harness-owned native evidence consumed during finalization
- `../launch/AGENTS.md` — writes spawn state via `SpawnStore` during launch pipeline
