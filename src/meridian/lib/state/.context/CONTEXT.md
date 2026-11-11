# lib/state/ — Context

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

### Relocate rows

Relocate rows rewrite recorded paths and the native store, with explicit keep
paths for checkouts that stay put. Rebasing is idempotent even when the destination
is inside the source, so a crash can be retried. Spawn rows, work items and task
links are not session events; the relocation operation updates them separately.
Older builds ignore the event and retain the old paths.

### One-time native identity import

The one-time import records unresolved chats in an atomic marker; ordinary reads
never repair missing keys. It validates native sources before locking, then
rechecks identity under the session lock. Unsupported rows or unavailable sources
defer import without a marker, and restored historical records stay inert. OpenCode
validation opens the live database read-only; SQLite may touch WAL shared-memory
read marks, but not native source bytes.

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

## Patterns

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

Per-chat work links follow moves, so running spawns need no deferral. A rename
forwards the old link to the new item, allowing a retry to finish after the move.
A crash between the move and forwarding leaves no evidence for that retry: rerun
from the new name. A done item keeps its checkout for agents still using it.

## Reference details

### Work-item metadata and project identity

`project_identity.py` is the only identity reader and writer. Its named minting
seam atomically writes init scaffolds with their committed identity. A root
containing `meridian.toml` or `mars.toml` uses committed identity, whose ID may
be absent until a write; a marker-less root gets a stable `adhoc.<slug>.<hash>` ID
derived from its canonical path. Ad-hoc
state is materialized only on writes, under `~/.meridian/projects/<id>/`, and
nothing is written into the source directory. Projects with a committed ID can move without
losing runtime history; moving an ad-hoc directory changes its identity. Project
lifetime gates sit outside the deletable project root and are never unlinked.
Real-launch materialization pre-creates the exact lock inode;
launch bind only projects that path and remains write-free.

Work items live under the configured `[context.work]` root. Marked projects may
use project-relative paths; ad-hoc read resolution does not fall back to them.
See `docs/configuration.md` for context-path resolution.

Work metadata reads tolerate, preserve, and warn about unknown fields. A newer `v`
gets a compatibility marker displayed as `requires-newer-meridian`; it never
collides with the stored status, and mutations validate every located copy before
touching disk. Malformed metadata still projects directory defaults, but always
warns with the file path. Repeated reads warn once per file per command.


### Spawn mutation seam

See [state rules](../AGENTS.md) for locking and published-artifact mutation.
Scope registration refuses terminal rows and cleanup claims, so new targets cannot
appear after cleanup has fixed its snapshot. The spawn repository and scope projection
are persistence leaves: cross-leaf operations belong in `spawn_aggregate.py`.

### Launch ownership

See [state rules](../AGENTS.md) for runner-lifetime lock ownership. Explicit
continue reuses the spawn and its lock. Lockless legacy rows alone have a bounded
pre-worker PID-less grace period; a free lock-backed launch derives terminal.

### History discovery and retention


Machine-wide retention has disjoint owners: committed-root scanning rejects
`adhoc.*`, while ad-hoc scanning accepts only that prefix. Ad-hoc activity is the
latest tree mtime under `projects/<id>/` or `context/<id>/`, including
`origin.json`, plus persisted spawn/session start and finish times. Pruning
rechecks eligibility under the exclusive project-lifetime gate and removes
`projects/<id>/` with `context/<id>/`.
`state.adhoc_retention_days` is user-config/environment-only; cwd project config
must never control a machine-wide scan.

See [history.md](history.md) for discovery, projection and restore contracts.

### Reconciliation: repair and cleanup


[State rules](../AGENTS.md) distinguish projected status from authority to
finalize or signal. Doctor captures orphan primaries only after its full walk and
defers while another row bound to the native session is active. Scope termination
requires positive runner-death evidence; unknown identity or access errors do not
authorize signals, even when replaying a cleanup claim.

### Native chat identity


A chat's native binding is immutable apart from a store relocation; callers
must use the accepted binding, not a rejected candidate. A later accepted start
projects earlier unstopped generations as stopped: the chat lifetime lock proves
the previous owner has released it. Fork and context-source ancestry belong to
the chat, not its latest generation.

### Chat→spawn link

A session start names the spawn running that generation. Ownership lookup does not
scan for a fallback, so a missing link leaves spawn commands unable to resolve the
chat. A child that never started has no session of its own and cannot be continued;
legacy placeholder rows need the same treatment. A generation belongs to a spawn
only when its `spawn_id` matches and its `history_id` is absent or matches: `/clear`
creates a chat with its own history. See the KB's `launch/session-references.md`
for why ownership starts with the session.

## Related .context/

- [../../harness/.context/CONTEXT.md](../../harness/.context/CONTEXT.md) — `SpawnExtractor` contract and harness-owned native evidence
- [../../launch/.context/CONTEXT.md](../../launch/.context/CONTEXT.md) — launch pipeline that writes spawn state via `SpawnStore`; composition seam, prepare/bind split
- [../../platform/.context/CONTEXT.md](../../platform/.context/CONTEXT.md) — `lock_file()`
  implementation details, Windows/POSIX branching
