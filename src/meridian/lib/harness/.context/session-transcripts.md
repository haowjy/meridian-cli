# lib/harness/ — Session Transcripts

`transcript.py` is the cross-harness read path for session data. It is independent of
the spawn/write paths: it only reads JSONL event files that harnesses have already
written.

## Canonical Tool Calls

`ToolCall` is the harness-agnostic representation of a tool invocation:

```python
class ToolCall(NamedTuple):
    name: str   # Canonical lowercase: bash, read, write, edit, grep, stdin, tool
    body: str   # Meaningful payload: command string, file path, pattern, etc.
```

`_normalize_tool(name, body) → ToolCall` maps raw harness-specific tool names onto this
canonical form. Downstream consumers (session-log rendering) use `ToolCall.name` and
`ToolCall.body` without knowing which harness produced the event.

| Raw harness name(s) | Canonical `name` | `body` |
|---|---|---|
| `bash` | `bash` | command string |
| `exec_command`, `shell`, `terminal`, `run_command` | `bash` | extracts `cmd` field from Codex JSON body, falls back to raw body |
| `write_stdin` | `stdin` | `""` (stdin interaction marker — no meaningful body) |
| `read`, `write`, `edit`, `grep` | same (lowercase) | path / pattern / description |
| anything else | lowercased name, or `"tool"` if empty | raw body |

## Messages and Providers

`TranscriptMessage` carries a tool invocation when `tool_call` is set, and marks tool
results with `is_tool_result=True`. Text-only messages leave both at their defaults
(`None` / `False`). These fields are the typed surface callers use to distinguish
conversation content from tool use — do not re-parse `content` when `tool_call` is
available.

Three providers handle different on-disk layouts. `transcript.py` selects the correct
one from the path:

| Provider | When selected | What it reads |
|---|---|---|
| `HistoryJsonlTranscriptProvider` | `path.name == HISTORY_FILENAME` | Crash-tolerant history via `iter_history_events()` |
| `OpenCodeStorageTranscriptProvider` | OpenCode storage paths | OpenCode SQLite/JSONL layout |
| `JsonlTranscriptProvider` | everything else | Raw JSONL, one event per line |

Callers use `iter_transcript_events(path)` or `parse_transcript_file(path)`; they never
select a provider directly.

## Compaction Segments

`segment_setups` holds the setup/handoff text for each compaction segment (one slot per
segment, `None` if absent). `consumed_setup_event_indexes` identifies raw event indexes
consumed by setup extraction. Callers that iterate the raw event list beside parsed
segments use it to avoid double-counting those events in the message stream.

## Related Context

- [CONTEXT.md](CONTEXT.md) — shared harness contracts
