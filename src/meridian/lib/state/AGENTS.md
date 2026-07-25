# lib/state/ — State Layer

File-backed authority for all Meridian runtime state. No database, no service,
no in-memory objects that survive process death. If it's not on disk, it doesn't exist.

## Roots

State splits across distinct roots — understand which goes where before writing anything:

```
meridian.toml
  [project] id                      — committed machine-managed project identity

~/.meridian/projects/.locks/<id>.lock
                                    — shared session-lifetime / exclusive deletion gate

~/.meridian/projects/<id>/          ← user runtime, never committed
  sessions.jsonl                    — session events (append-only)
  locks/spawns/<spawn_id>.lock      — stable per-spawn external-writer lock
  locks/process-scopes/<spawn_id>.lock
                                    — stable process-scope sidecar mutation lock
  locks/reaper-cleanup/<spawn_id>.lock
                                    — stable reaper cleanup lock
  locks/launch-boundary/<spawn_id>.lock
                                    — stable launch-boundary append lock
  locks/gc.lock                     — lock-GC pass serialization
  spawns/.staging/<unique>/         — complete row build before atomic publication
  spawns/<spawn_id>/
    state.json                      — authoritative spawn state (schema v3)
    history.jsonl                   — primary output artifact
    attempt-N/                      — preserved retry evidence from prior attempts
    last-observed-event.json        — diagnostic marker for last harness event + counters
    runner-lifecycle.jsonl          — runner breadcrumb journal (signals, phases, atexit)
    finalize-evidence.json          — orphan-time liveness snapshot before reaper cleanup
    process_scopes.json             — durable process identities + release markers
    reaper_cleanup_claim.json       — pending finalize-first cleanup targets
    heartbeat · report.md · stderr.log · params.json · tokens.json

<context.work root>/<slug>/         ← context-resolved, NOT repo-local
  __status.json                     — mutable per-work-item metadata
  prompts/ handoffs/ …              — work artifacts
```

The project ID in `meridian.toml` `[project] id` keys into `~/.meridian/projects/<id>/`. Projects
can move or be renamed without losing runtime history. Project lifetime gates sit outside
the deletable project root and are never unlinked.

Work items live under the `[context.work]` root (default
`{user_home}/context/<id>/work/<slug>/`), resolved by `work_scope.py` /
`work_state.py` — **never** the project repo. See `docs/configuration.md` for
context-path resolution.

## Spawn State: V2 Per-Spawn Files

Spawn state lives in individual `state.json` files (`spawns/<id>/state.json`),
not a global event log, so status reads stay O(1) instead of replaying O(n) events.

## Spawn Mutation Seam

Every update to a published spawn calls `write_state_locked()`. It acquires
`locks/spawns/<id>.lock`, re-reads current state, applies a pure mutator, and writes
atomically. The lock identity is outside the artifact directory it protects and remains
stable while the spawn exists. Orphaned identities are unlinked only through lock GC's
validated-exclusive, unlink-before-release seam.

Process-scope registration and release markers route through the locked sidecar
mutation seam. When both locks are needed, acquire the spawn-state lock before the
scope-sidecar lock. Registration is refused after the spawn becomes terminal or a
cleanup claim exists, so a process scope cannot appear after cleanup targets are fixed.
Any writer into a destructible `spawns/<id>/` directory that can outlive deletion must
not recreate its owner. Parent-creating mutations take the spawn-state lock and refuse
a missing published row through `mutate_published_spawn_artifact()`; signals, scope,
history, launch/reaper diagnostics, failure sentinels, and harness journals use this
seam. Heartbeats never create a missing parent directory and stop after deletion.
The spawn repository and process-scope projection are persistence leaves with a
one-way dependency: the projection may use repository reads and lock paths, while
the repository never imports the projection. Cross-leaf operations belong in the
aggregate `spawn_aggregate.py`; in particular, published-spawn deletion owns the
lock order above and removes both `spawns/<id>/` and any legacy
`artifacts/<id>/` directory. Reaper claims consume one immutable projection snapshot containing
both scopes and released IDs, read under a single projection-lock acquisition.

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

A crash in the middle of a plain `open()` write leaves a partial file; partial
`state.json` fails Pydantic validation on next read.

## Read vs Write Root Resolvers

Use the correct resolver — they have different side effects:

| Resolver | Creates UUID? | Use when |
|---|---|---|
| `resolve_project_runtime_root()` | No | Read paths (list, show, status) |
| `resolve_project_runtime_root_for_write()` | Yes (under lock) | Write paths only |

Using `*_for_write()` on a read path creates `meridian.toml` identity in untouched checkouts,
triggering project setup side effects in CI.

## Reconciliation Behavior

`reaper.py:reconcile_spawns()` accepts and returns an immutable `SpawnScan`, preserving
its quarantine partition while projecting stale active records for list, stats, reference,
and dashboard callers. It may return an in-memory terminal status but never persists
state or terminates processes, and it is safe at any depth.

`reconcile_active_spawn()` is the side-effectful repair path. It runs from doctor
background repair and fails closed outside root depth. It snapshots a cleanup claim
under the spawn lock, persists the terminal outcome through the locked external-writer
path, then terminates the claimed, birth-validated scopes. A separate stable cleanup
lock prevents concurrent reapers from double-signalling; terminal rows retain failed
claims for the next doctor pass.
Both paths share liveness rules in `reaper.py` and completion/cancel precedence in
`reconciliation.py`.

## Entry Points

- `user_paths.py` — `get_user_home()`. Start here for any new user-level storage.
- `paths.py` — `RuntimePaths`, read vs write root resolvers.
- `spawn_store.py` — listing/query, creation, and mutation interface; scans return `SpawnScan`.
- `spawn_aggregate.py` — published-row deletion and spawn-owned artifact lifetime guard.
- `work_state.py` / `work_store.py` / `work_repository.py` — work-item models and
  codec, pure reads, and the single locked mutation repository, respectively.
- `session_store.py` — Session event log.
- `atomic.py` — atomic write primitives. All state writes use these.
- `reaper.py` — read-only `reconcile_spawns()` projection and root-only
  `reconcile_active_spawn()` repair.
- `reconciliation.py` — shared reconciliation decisions and completion/cancel precedence.

## Spawn Subpackage

`spawn/` contains domain models, strict v3 persistence helpers, the one-shot legacy
v2 read upgrader, and pure transitions.
→ [spawn/AGENTS.md](spawn/AGENTS.md)

## Anti-Patterns

**Don't read `state.json` without `read_state()`** — raw JSON reads bypass Pydantic
validation and miss `SpawnRecord` reconstruction from `starting-prompt.md`.

**Don't use `*_for_write()` on read paths** — creates project identity in clean checkouts.

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
repository. Pruning acquires `spawns_flock` first. Pending reaper cleanup
claims block deletion so durable cleanup intent is never discarded.

**Don't hardcode `~/.meridian/`** — use `get_user_home()` from `user_paths.py`.
It honors `MERIDIAN_HOME` and centralizes home resolution.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — full dual-root layout, per-spawn state,
   monotonic ID generation, terminal write authority lattice, and worktree metadata.

## Related

- `../harness/AGENTS.md` — `ArtifactStore` protocol consumers that read from this layer
- `../launch/AGENTS.md` — writes spawn state via `SpawnStore` during launch pipeline
