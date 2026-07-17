# lib/state/ — State Layer

File-backed authority for all Meridian runtime state. No database, no service,
no in-memory objects that survive process death. If it's not on disk, it doesn't exist.

## Roots

State splits across distinct roots — understand which goes where before writing anything:

```
.meridian/                          ← repo-local, committed scaffolding
  id                                — project UUID / three-word ID

~/.meridian/projects/<id>/          ← user runtime, never committed
  sessions.jsonl                    — session events (append-only)
  spawns/.staging/<unique>/         — complete row build before atomic publication
  spawns/<spawn_id>/
    state.json                      — authoritative spawn state (v2)
    state.lock                      — per-spawn lock for external writers
    history.jsonl                   — primary output artifact
    heartbeat · report.md · stderr.log · params.json · tokens.json

<context.work root>/<slug>/         ← context-resolved, NOT repo-local
  __status.json                     — mutable per-work-item metadata
  prompts/ handoffs/ …              — work artifacts
```

The project UUID in `.meridian/id` keys into `~/.meridian/projects/<id>/`. Projects
can move or be renamed without losing runtime history.

Work items live under the `[context.work]` root (default
`{user_home}/context/<id>/work/<slug>/`), resolved by `work_scope.py` /
`work_store.py` — **never** the project repo. See `docs/configuration.md` for
context-path resolution.

## Spawn State: V2 Per-Spawn Files

Spawn state lives in individual `state.json` files (`spawns/<id>/state.json`),
not a global event log, so status reads stay O(1) instead of replaying O(n) events.

## Two Write Tiers

**Tier 1 — Owner writes (unlocked):**
The spawn's own runner calls `write_state()` directly. It is the sole writer while
active. Includes a best-effort terminal monotonicity guard — will not overwrite an
already-terminal record unless `allow_terminal_overwrite=True`.

**Tier 2 — External writes (per-spawn lock):**
The reaper, cancel command, or any other process mutating a spawn it doesn't own
calls `write_state_locked()`. This acquires `spawns/<id>/state.lock`, reads current
state, applies a mutator function, and writes atomically. Skipping the lock races
with the owner's unlocked writes.

## Atomic Write Contract

All state writes go through `atomic.py`. Never write state files with plain `open()`.

- `atomic_write_text()` / `atomic_write_bytes()` — write to same-directory temp,
  `os.fsync()`, then `os.replace()` (atomic rename). Either the old or new file
  exists — never a partial write.
- `atomic_publish_dir()` — rename a complete same-volume stage into a destination
  that must not exist, then fsync the publication parent.
- `append_text_line()` — binary mode so `\n` is never translated to `\r\n` on
  Windows. JSONL byte offsets must be stable across platforms.

A crash in the middle of a plain `open()` write leaves a partial file; partial
`state.json` fails Pydantic validation on next read.

## Read vs Write Root Resolvers

Use the correct resolver — they have different side effects:

| Resolver | Creates UUID? | Use when |
|---|---|---|
| `resolve_project_runtime_root()` | No | Read paths (list, show, status) |
| `resolve_project_runtime_root_for_write()` | Yes (under lock) | Write paths only |

Using `*_for_write()` on a read path creates `.meridian/id` in untouched checkouts,
triggering project setup side effects in CI.

## Reconciliation Behavior

`reaper.py:reconcile_spawns()` gives list, stats, reference, and dashboard callers
a read-only projection of stale active rows. It may return an in-memory terminal
status but never persists state or terminates processes, and it is safe at any depth.

`reconcile_active_spawn()` is the side-effectful repair path. It runs from doctor
background repair, fails closed outside root depth, cleans recorded orphan process
scopes, and persists the terminal outcome through the locked external-writer path.
Both paths share the liveness and completion decision rules in `reaper.py`.

## Entry Points

- `user_paths.py` — `get_user_home()`. Start here for any new user-level storage.
- `paths.py` — `RuntimePaths`, read vs write root resolvers.
- `spawn_store.py` — `SpawnStore`. Main interface for listing, creating, updating spawns.
- `session_store.py` — Session event log.
- `atomic.py` — atomic write primitives. All state writes use these.
- `reaper.py` — read-only `reconcile_spawns()` projection and root-only
  `reconcile_active_spawn()` repair.

## Spawn Subpackage

`spawn/` contains domain models, v2 persistence helpers, and finalization policy.
→ [spawn/AGENTS.md](spawn/AGENTS.md)

## Anti-Patterns

**Don't read `state.json` without `read_state()`** — raw JSON reads bypass Pydantic
validation and miss `SpawnRecord` reconstruction from `starting-prompt.md`.

**Don't use `*_for_write()` on read paths** — creates project UUID in clean checkouts.

**Don't write state files with `open()`** — use `atomic_write_text()` or `append_text_line()`.

**Don't acquire `spawns_flock` for per-spawn mutations** — the global lock serializes
spawn ID allocation, initial row publication, and abandoned-stage GC. Later mutations
use `write_state_locked()` (`state.lock`).

**Don't hardcode `~/.meridian/`** — use `get_user_home()` from `user_paths.py`.
It handles `MERIDIAN_HOME`, Windows `%LOCALAPPDATA%`, and POSIX `~` correctly.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — full dual-root layout, per-spawn state,
   monotonic ID generation, terminal write authority lattice, and worktree metadata.

## Related

- `../harness/AGENTS.md` — `ArtifactStore` protocol consumers that read from this layer
- `../launch/AGENTS.md` — writes spawn state via `SpawnStore` during launch pipeline
