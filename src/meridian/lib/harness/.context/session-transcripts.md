# lib/harness/ — Session Transcripts

`transcript.py` is the cross-harness read path for session data. It is independent of
the spawn/write paths and reads existing provider storage: Meridian history JSONL,
generic native JSONL, or OpenCode SQLite/legacy JSON storage.

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
| `OpenCodeStorageTranscriptProvider` | OpenCode storage paths | Read-only OpenCode SQLite first, then legacy JSON storage; a present empty DB session is authoritative |
| `JsonlTranscriptProvider` | everything else | Raw JSONL, one event per line |

Callers use `iter_transcript_events(path)` or `parse_transcript_file(path)`; they never
select a provider directly.

Before native-provider dispatch, the reader recognizes a sealed Meridian native
snapshot by its bounded storage header (including renamed explicit files), or by
the reserved `native-transcript.jsonl` name. State's snapshot codec unwraps and
validates it; native dictionaries still use the existing normalizer. Storage
headers/seals never become conversational events. A shared `TranscriptValidation`
stays partial until verified EOF, separately from `rendering_reason`; search
withholds unverified matches and preview refresh does not cache partial reads as
current. Capture qualification: `transcript_capture.py` streams, hashes, and records
status (complete, known-incomplete, unavailable, or unsupported);
`capture_qualify.py` observers own dialect tails. Only complete observations are sealed into
`native-transcript.jsonl`. JSONL capture keeps raw lines; OpenCode keeps versioned
raw-row envelopes. Unfinished dialect tails refuse publication.
Resolved native files are distinct from explicit file inputs: their known native
session/harness binding is checked when a storage header is encountered. Snapshots
never use append checkpoints, even when their enclosing target is spawn-owned.

## Compaction Segments

`segment_setups` holds the setup/handoff text for each compaction segment (one slot per
segment, `None` if absent). `consumed_setup_event_indexes` identifies raw event indexes
consumed by setup extraction. Callers that iterate the raw event list beside parsed
segments use it to avoid double-counting those events in the message stream.

## Provider-Specific Contracts

### Pi Journals and Rendering Limits

Pi native `message` and RPC `message_end` share message extraction. Native
compactions create segments with recorded summaries; branch summaries and parent
changes are typed annotations in append order, not reconstructed active context.
The normalizer carries preceding-entry identity across preview checkpoints.
Unknown material records/content set `rendering_reason`; consumers must not call
that a complete empty rendering. Raw storage remains unchanged.

### OpenCode Raw Transcript Rows

The DB provider emits `record=opencode.transcript`, version 1: one session row,
message rows with their raw part rows, then unassociated session parts. All native
columns and original payload strings survive; blobs use a typed base64 value.
Session existence and the complete row read share one read-only transaction.
Missing sources and DB errors must not become empty iteration. A present DB session
wins over legacy JSON even when empty.

Only the shared normalizer translates these records. It preserves initial-user
setup and summary compactions across preview checkpoints; unsupported roles,
parts and malformed material retain an explicit rendering reason. Rendering
support is not native capture qualification: a consistent DB transaction can
still contain unfinished work. Do not use normalized display events as capture
input or treat iterator creation as completed source validation.

## Related Context

- [CONTEXT.md](CONTEXT.md) — shared harness contracts
