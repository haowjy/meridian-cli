# harness/extractors/ — Context

## Architecture

`HarnessExtractor` extends `SpawnExtractor` (from `adapter.py`) with two additional
extraction paths that `SpawnExtractor` does not define:

```
SpawnExtractor (artifact-based, post-completion)
  extract_session_id(artifacts, spawn_id)
  extract_usage(artifacts, spawn_id)
  extract_report(artifacts, spawn_id)

HarnessExtractor (adds live-event and planned-identity paths)
  detect_session_id_from_event(event)       ← live, per-event
  detect_session_id_from_artifacts(spec, launch_env, child_cwd, runtime_root)
                                             ← already-planned identity, never discovery
```

The `observe_session_id()` priority chain in the parent adapter calls these in order —
see parent [`.context/CONTEXT.md`](../../.context/CONTEXT.md) for the full chain.

## Contracts

### `detect_session_id_from_event(event)`

Best-effort. Returns `None` when the event carries no session information — the caller
tries the next step in the priority chain. Never raises. The event comes from the live
connection drain loop; call cost must be low.

Codex accepts `thread.started`/`thread/started` and `session_id` identity
frames, using only their envelope fields or the qualified thread object. Assistant
text, tool payloads, and identity-shaped nested keys are not identity evidence.
OpenCode uses its event-envelope parser in `opencode_report.py`, not recursive
key search. Owned connection API responses can provide the ID before any stream
frame arrives.

### `detect_session_id_from_artifacts(spec, launch_env, child_cwd, runtime_root)`

This compatibility port can return an already-planned identity. It must not scan
native stores, logs, cwd matches, timestamps, or output prose for a tracked ID.
Exact native reads belong to the adapter's recorded-store resolver, not extractors.

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
planned-identity lookup local to the extractor. Do not reintroduce duplicate
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
