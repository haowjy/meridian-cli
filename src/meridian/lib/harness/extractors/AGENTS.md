# harness/extractors/

Session ID, token usage, and report extraction from harness processes and artifacts.
Each harness has one extractor that serves both the subprocess path and the streaming path.

## Files

- `base.py` — `HarnessExtractor` Protocol (extends `SpawnExtractor`); helper utilities
  `session_from_mapping_with_keys`, `normalize_harness_event_type`
- `claude.py` — `ClaudeHarnessExtractor`
- `codex.py` — `CodexHarnessExtractor`
- `opencode.py` — `OpenCodeHarnessExtractor`
- `__init__.py` — empty package stub

## Entry Points

Extractors are not instantiated directly by callers. Each adapter registers its extractor
in `HarnessBundle`. Access via `adapter.extractor` or `bundle.extractor` after bootstrap.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — two extraction paths (live event vs
  artifacts), per-harness session ID key names, fallback detection logic, protocol
  runtime-checkability

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — `observe_session_id()` priority
  chain that drives which extractor methods are called and when
- [../connections/AGENTS.md](../connections/AGENTS.md) — `HarnessEvent` produced by
  connections is passed to `detect_session_id_from_event`
