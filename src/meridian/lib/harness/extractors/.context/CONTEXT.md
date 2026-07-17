# harness/extractors/ — Context

## Architecture

`HarnessExtractor` extends `SpawnExtractor` (from `adapter.py`) with two additional
extraction paths that `SpawnExtractor` does not define:

```
SpawnExtractor (artifact-based, post-completion)
  extract_session_id(artifacts, spawn_id)
  extract_usage(artifacts, spawn_id)
  extract_report(artifacts, spawn_id)

HarnessExtractor (adds live-event and filesystem-scan paths)
  detect_session_id_from_event(event)       ← live, per-event
  detect_session_id_from_artifacts(spec, launch_env, child_cwd, runtime_root)
                                             ← filesystem scan, harness home dirs
```

The `observe_session_id()` priority chain in the parent adapter calls these in order —
see parent [`.context/CONTEXT.md`](../.context/CONTEXT.md) for the full chain.

## Contracts

### `detect_session_id_from_event(event)`

Best-effort. Returns `None` when the event carries no session information — the caller
tries the next step in the priority chain. Never raises. The event comes from the live
connection drain loop; call cost must be low.

Per-harness session ID key names searched (via `session_from_mapping_with_keys`):

| Harness | Keys searched (in order, recursive) |
|---|---|
| Claude | `sessionId`, `session_id` |
| Codex | `threadId`, `thread_id`, `session_id`, `sessionId`, `sessionID` |
| OpenCode | `session_id`, `session`, `sessionId`, `id` |
| Pi | `id` (from `session` event type on stdout) |

`session_from_mapping_with_keys` searches recursively into nested dicts — it will find
a session ID inside deeply nested payloads. This catches edge cases but can also pick up
session IDs from unrelated nested objects. If multiple nested dicts contain different
session IDs, the first match wins.

### `detect_session_id_from_artifacts(spec, launch_env, child_cwd, runtime_root)`

Filesystem scan. Called when the live-event path returns None and no `session_id.txt`
artifact exists. Each extractor scans the harness's home directory for recent session
files and matches against `child_cwd`:

- **Claude**: scans `~/.claude/projects/<slugified-cwd>/` for `.jsonl` files; reads
  `sessionId` from the first line or falls back to the file stem as the session ID
- **Codex**: scans `<codex_home>/sessions/rollout-*.jsonl`; calls
  `resolve_rollout_session_id(path, project_root)` to confirm match
- **OpenCode**: parses log lines matching `service=session ... directory=<cwd> ... created`
  within a 15-minute window of `started_at_epoch`
- **Pi**: scans `~/.meridian/meridian-pi/sessions/` for `*.jsonl` files whose first line
  is a `session` event with matching `cwd`. The Pi session directory is configurable via
  `PI_CODING_AGENT_SESSION_DIR` env var. Session files use `--` as path separator
  (e.g., `home--jimyao--gitrepos--myproject--.jsonl`). The legacy native-Windows
  branch uses case-insensitive cwd matching and is untested.

### Pi Session CWD Encoding

Pi session files encode the cwd as a slugified path: `/` replaced with `--`, with a
trailing `--`. The extractor normalizes both the launch cwd and the file-derived cwd
through `_safe_resolve()` → `replace("\\", "/")` → `rstrip("/")` before comparison.
This keeps persisted path identity comparisons separator-stable.

This path is inherently racy — it relies on the harness having written files by the
time this runs. The 15-minute window for OpenCode and the mtime-sorted scan for Claude/
Codex are heuristics, not guarantees.

### `extract_session_id(artifacts, spawn_id)` / `extract_usage` / `extract_report`

These operate on `ArtifactStore` — read from persisted spawn artifacts, not live
process state. Called after the process has completed (subprocess path) or after the
drain loop exits (streaming path). `ArtifactStore` is the abstraction — do not reach
for raw file paths.

Report extraction must preserve the same parent-scope boundary as terminal
classification. OpenCode's global event stream can include child task sessions; its
report extractor resolves the parent session from `session_id.txt`, parent terminal
events, or the first parent user `message.updated`, then ignores child-session
assistant text. Child task output stays readable through `meridian session log`, but
it must not become the parent `report.md`.

OpenCode session-id/report parsing is owned by `harness/opencode_report.py`.
`extractors/opencode.py` delegates to that module for artifact session-id detection
and report extraction, while keeping live-event detection, usage extraction, and
filesystem/database discovery local to the extractor. Do not reintroduce duplicate
OpenCode event parsers in the generic `harness/common.py` helpers.

### Protocol is `@runtime_checkable`

`isinstance(obj, HarnessExtractor)` works. The check tests for the presence of the
protocol methods. It does not verify signature compatibility. Do not rely on
isinstance to validate that an implementation is correct — use it only to confirm the
API surface is present.

### `normalize_harness_event_type(payload, keys)`

Normalizes raw event type strings to dot-separated lowercase. Input variation examples:
- `"turn/completed"` → `"turn.completed"`
- `"session.idle"` → `"session.idle"`
- `"result"` → `"result"`

Used for consistent lookup in dictionaries keyed by normalized event types. Not all
callers use this — `event.event_type` is the raw form and callers that branch on it
must account for the harness-specific raw format.

## Related .context/

- [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — `observe_session_id()` priority
  chain; `ArtifactStore` contract
- [../../connections/.context/CONTEXT.md](../../connections/.context/CONTEXT.md) — `HarnessEvent`
  structure and `event_type` namespace scoping
