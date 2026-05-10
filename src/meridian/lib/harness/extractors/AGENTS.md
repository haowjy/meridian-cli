# harness/extractors/ — Result Extraction

Session ID, token usage, and report extraction from harness output. One extractor
per harness, serving both the subprocess path (artifacts on disk) and the streaming
path (live `HarnessEvent` objects).

## Mental Model

Extractors sit at the end of a spawn: after the process exits or the connection
closes, something must pull session IDs, cost data, and status reports from what
the harness produced. Extractors do that work without knowing whether the harness
ran as a subprocess or a connection.

Two extraction modes:
- **Live event mode**: `detect_session_id_from_event(event)` — called per event
  during the drain loop when a session ID is not yet known.
- **Artifact mode**: `extract_session_id()`, `extract_usage()`, `extract_report()`
  — called post-exit on `history.jsonl` and output artifacts.

## Key Rules

**Extractors are not instantiated by callers directly.** Each adapter registers its
extractor in `HarnessBundle`. Access via `adapter.extractor` or `bundle.extractor`
after `ensure_bootstrap()`.

**Session ID key names differ per harness.** Claude writes `sessionId`; Codex uses
`session_id`; OpenCode uses a different path. The `session_from_mapping_with_keys`
helper in `base.py` handles the per-harness key lookup.

**Protocol is runtime-checkable.** `HarnessExtractor` extends `SpawnExtractor` as a
`Protocol` — runtime `isinstance()` checks work. Add new methods to the Protocol
and to all three implementations together.

## Entry Points

- `base.py` — `HarnessExtractor` Protocol, `session_from_mapping_with_keys`,
  `normalize_harness_event_type`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — two extraction paths in detail,
   per-harness session ID key names, fallback detection logic.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — `observe_session_id()` priority
  chain that drives when and how extractor methods are called.
- [../connections/AGENTS.md](../connections/AGENTS.md) — `HarnessEvent` that
  `detect_session_id_from_event` receives.
