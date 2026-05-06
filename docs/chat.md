# Chat Backend

`meridian chat` starts a headless local server that exposes agent conversations
over REST and WebSocket. It is the programmatic backend for browser-based UIs,
custom tooling, and any client that needs a structured event stream instead of
a raw terminal.

## Start the Server

```bash
meridian chat                          # Claude, random port
meridian chat --harness codex          # Codex
meridian chat --harness opencode       # OpenCode
meridian chat --model gpt-4o           # explicit model
meridian chat --port 8765              # fixed port
meridian chat --port 8765 --host 0.0.0.0  # listen on all interfaces
meridian chat --no-headless              # frontend placeholder; still API-only for now
```

On startup, the server prints its base URL and blocks:

```
Chat backend: http://127.0.0.1:52341
```

### Flags

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--harness NAME` | `claude` | Harness to use: `claude`, `codex`, `opencode` |
| `--model NAME` | harness default | Model id or alias |
| `--port PORT` | `0` (auto) | Port to bind; `0` picks a free port |
| `--host HOST` | `127.0.0.1` | Interface to bind |
| `--headless/--no-headless` | `--headless` | API-only mode. `--no-headless` currently prints a frontend-unavailable notice and continues headless. |

Global `--harness` is read from `meridian config` when `--harness` is omitted.

Startup writes `~/.meridian/chat-server.json` with the current base URL so management
commands can find the running server. Pass `--url` to any management command to
override discovery.

### Usage context

`meridian chat` and all its subcommands (`ls`, `show`, `log`, `close`) are for
**root-process use only**. Running them inside a spawn (where `MERIDIAN_DEPTH > 0`)
exits immediately with a clear error. Start the chat server from your terminal or
a top-level process, not from within an agent.

---


## Management CLI

These commands connect to a running chat server. By default they read the server
URL from `~/.meridian/chat-server.json`; use `--url http://host:port` to target a
specific server.

```bash
meridian chat ls
meridian chat show c-a1b2c3
meridian chat log c-a1b2c3 --last 20
meridian chat log c-a1b2c3 --follow
meridian chat close c-a1b2c3
```

- `ls` prints `chat_id | state | created_at`.
- `show` prints state and the last few events.
- `log` prints event JSON; `--follow` tails live events over WebSocket after
  replaying the requested history.
- `close` posts to `/chat/{chat_id}/close` and confirms accepted closes.

---

## Chat Lifecycle

A chat is a persistent conversation backed by one agent process. Backend is
acquired on the first prompt, not on creation.

```
POST /chat              →  reserve chat_id (no agent starts yet)
POST /chat/{id}/msg     →  cold-start the harness, deliver first prompt
                           subsequent prompts reuse the same backend
POST /chat/{id}/cancel  →  interrupt the current turn
POST /chat/{id}/close   →  end the conversation (agent process exits)
```

### Chat States

| State | Meaning |
| ----- | ------- |
| `idle` | Created or turn complete, waiting for next prompt |
| `active` | Turn in progress |
| `draining` | Cancel requested, draining remaining output |
| `closed` | Conversation ended; replay still available from event log |

---

## REST API

All endpoints return JSON. Error responses use `{"detail": "<reason>"}`.

### List chats

```
GET /chat
```

Response:
```json
{
  "chats": [
    {
      "chat_id": "c-a1b2c3...",
      "state": "idle",
      "created_at": "2026-04-30T12:00:00Z"
    }
  ]
}
```

### Create a chat

```
POST /chat
```

Body (optional):
```json
{ "model": "claude-opus-4", "harness": "claude" }
```

Response:
```json
{ "chat_id": "c-a1b2c3...", "state": "idle" }
```

### Send a prompt

```
POST /chat/{chat_id}/msg
```

Body:
```json
{ "text": "Summarize the microct scan in data/scan.tiff" }
```

Response: `{"status": "accepted"}` or `{"status": "rejected", "error": "<reason>"}`

### Cancel the current turn

```
POST /chat/{chat_id}/cancel
```

No body. Interrupts the running turn; chat transitions to `draining` then `idle`.

### Approve or reject an agent request (HITL)

Supported on Codex. Claude and OpenCode do not support runtime approvals.

```
POST /chat/{chat_id}/approve
```

Body:
```json
{
  "request_id": "req-abc123",
  "decision": "accept",
  "payload": {}
}
```

`decision` is `"accept"` or `"reject"`. `payload` is optional extra context for
the harness.

### Answer agent questions

```
POST /chat/{chat_id}/input
```

Body:
```json
{ "request_id": "req-abc123", "answers": { "key": "value" } }
```

### Revert to a checkpoint

```
POST /chat/{chat_id}/revert
```

Body:
```json
{ "commit_sha": "abc1234" }
```

Restores git working tree to the checkpoint created at the end of the named
turn. See [Checkpoints](#checkpoints).

### Close a chat

```
POST /chat/{chat_id}/close
```

No body. Closes the agent process and marks the chat `closed`. Event log
remains readable for replay.

### List chat events

```
GET /chat/{chat_id}/events
GET /chat/{chat_id}/events?last=20
```

Returns persisted event log entries for replay or inspection. `last=0` returns
an empty list.

### Get chat state

```
GET /chat/{chat_id}/state
```

Response:
```json
{ "chat_id": "c-a1b2c3...", "state": "idle" }
```

---

## WebSocket

```
WS /ws/chat/{chat_id}
WS /ws/chat/{chat_id}?last_seq=42   # resume from seq 42 (reconnect)
```

The WebSocket is bidirectional:

- **Server → client**: `ChatEvent` frames (JSON objects)
- **Client → server**: `ChatCommand` frames (JSON objects)
- **Server → client**: `CommandAck` frames (acknowledgement per command)

### Reconnect / Replay

Pass `?last_seq=N` to receive all events from seq `N+1` onward. The server
replays from the persisted event log before switching to live delivery.
Omit `last_seq` to receive only events generated after connection.

### Sending Commands over WebSocket

```json
{
  "command_type": "prompt",
  "command_id": "client-uuid-here",
  "chat_id": "c-a1b2c3...",
  "timestamp": "2026-04-30T12:00:00Z",
  "payload": { "text": "What changed in the last turn?" }
}
```

Required fields: `command_type`, `command_id`, `chat_id`, `timestamp`, `payload`.

Acknowledgement:
```json
{ "ack": "client-uuid-here", "status": "accepted" }
{ "ack": "client-uuid-here", "status": "rejected", "error": "<reason>" }
```

### Command Types

| `command_type` | Payload fields | Description |
| -------------- | -------------- | ----------- |
| `prompt` | `text` | Send a message and start a turn |
| `cancel` | — | Interrupt the current turn |
| `approve` | `request_id`, `decision`, `payload?` | Resolve an agent approval request |
| `answer_input` | `request_id`, `answers` | Answer agent questions |
| `close` | — | End the conversation |
| `revert` | `commit_sha` | Restore to a checkpoint |
| `swap_model` | `model` | Switch model for the next turn |
| `swap_effort` | `effort` | Switch effort/reasoning level for the next turn |

---

## Event Reference

Every event has this envelope:

```json
{
  "type": "turn.started",
  "seq": 14,
  "chat_id": "c-a1b2c3...",
  "execution_id": "chat-uuid...",
  "timestamp": "2026-04-30T12:00:01.234Z",
  "turn_id": "t-001",
  "item_id": null,
  "request_id": null,
  "payload": { ... },
  "harness_id": "claude"
}
```

`seq` is monotonically increasing per chat. Use it for `last_seq` on reconnect.

### Chat

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `chat.started` | — | Chat created |
| `chat.configured` | `model`, `harness` | Model and harness resolved |
| `chat.state_changed` | `state` | Lifecycle state transition |
| `chat.exited` | — | Conversation ended |

### Turn

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `turn.started` | `model`, `effort` | Prompt delivered, model responding |
| `turn.completed` | `outcome`, `usage`, `cost` | Turn finished (`completed` / `failed` / `interrupted` / `cancelled`) |

Meridian normalizes each harness's native stream into this turn contract. Clients
should treat `turn.completed` as the single turn-end signal, regardless of
whether the source harness was Claude, Codex, or OpenCode.

### Content

All content uses type `content.delta` with a `stream_kind` in the payload:

| `stream_kind` | Description |
| ------------- | ----------- |
| `assistant_text` | Response text from the model |
| `reasoning_text` | Thinking/reasoning |
| `reasoning_summary_text` | Compacted reasoning |
| `command_output` | Command stdout/stderr |
| `file_change_output` | File diff output |

Example:
```json
{
  "type": "content.delta",
  "payload": { "stream_kind": "assistant_text", "text": "Here is the analysis..." }
}
```

### Items (tool calls)

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `item.started` | `item_type`, `name` | Tool or action began |
| `item.updated` | `item_type`, `progress` | Progress update |
| `item.completed` | `item_type`, `outcome` | Finished |

`item_type` values: `command_execution`, `file_change`, `mcp_tool_call`,
`web_search`, `context_compaction`, `image_view`.

Current Claude, Codex, and OpenCode chat backends all map their live native tool
events into this `item.*` lifecycle, so clients should consume the canonical
events above rather than harness-specific raw shapes.

### Files

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `files.persisted` | `paths`, `operation` | Files written to disk during a turn |

### Spawns

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `spawn.started` | `spawn_id`, `agent`, `desc` | Sub-agent launched |
| `spawn.progress` | `spawn_id`, `summary` | Sub-agent progress update |
| `spawn.completed` | `spawn_id`, `outcome`, `summary` | Sub-agent done |

### HITL Requests

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `request.opened` | `request_id`, `request_type`, `detail` | Agent needs approval (`command`, `file_read`, `file_change`) |
| `request.resolved` | `request_id`, `decision` | Request resolved |
| `user_input.requested` | `request_id`, `questions` | Agent asking questions |
| `user_input.resolved` | `request_id`, `answers` | Questions answered |

HITL requests are active for Codex. Claude and OpenCode do not surface runtime
approval requests — they use launch-time permission settings instead.

### Checkpoints

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `checkpoint.created` | `commit_sha`, `turn_id` | Git snapshot at turn boundary |
| `checkpoint.reverted` | `commit_sha`, `turn_id` | Working tree restored |

### Runtime

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `runtime.warning` | `reason` | Non-fatal issue |
| `runtime.error` | `reason` | Error (provider / transport / permission) |

### Work, Model, Extension

| Type | Payload | Description |
| ---- | ------- | ----------- |
| `work.started` | `work_id` | Work item attached |
| `work.status_changed` | `status` | Work item status updated |
| `work.files_changed` | `paths` | Work directory files changed |
| `model.rerouted` | `from`, `to`, `reason` | Model changed mid-session |
| `extension.*` | varies | Domain-specific or harness-specific events |

`extension.*` follows the prefix convention: `extension.<domain>.<event>` for
domain events, `extension.<harness>.<event>` for harness-specific events (e.g.
`extension.claude.thinking_budget`).

---

## Checkpoints

The server creates a git commit at the end of every turn that produces file
changes. The `checkpoint.created` event includes the `commit_sha`.

To revert:

```bash
# via REST
curl -X POST http://localhost:8765/chat/c-abc/revert \
  -H 'Content-Type: application/json' \
  -d '{"commit_sha": "abc1234"}'
```

The revert restores the working tree to the checkpoint state and emits
`checkpoint.reverted`. Subsequent prompts continue from the reverted state.

---

## Persistence

Each chat stores events under `~/.meridian/chats/<chat_id>/`:

| File | Description |
| ---- | ----------- |
| `history.jsonl` | Append-only event log — source of truth |
| `index.sqlite3` | Derived SQLite index; rebuilt from JSONL if missing |

The JSONL log is crash-safe (atomic writes, tolerates truncation). Events are
never modified or deleted. `closed` chats remain readable for replay.

On server restart, all non-closed chats are recovered from their event logs.
Chats that were `active` or `draining` when the process died emit a
`runtime.error` event with `reason: backend_lost_after_restart`.

---

## Harness Support

| Harness | Runtime HITL | Model switching | Notes |
| ------- | ------------ | --------------- | ----- |
| Claude (`claude`) | No | Yes | Launch-time permissions only. Parses `--output-format stream-json`; live assistant/tool events normalize into canonical `turn.*`, `content.delta`, and `item.*` chat events. |
| Codex (`codex`) | Yes | Yes | Connects to Codex app-server via WebSocket; live assistant/tool events normalize into canonical `turn.*`, `content.delta`, and `item.*` chat events. |
| OpenCode (`opencode`) | No | Yes | Connects to OpenCode HTTP SSE API; live assistant/tool events normalize into canonical `turn.*`, `content.delta`, and `item.*` chat events. |

For Codex managed primary session behavior, see [codex-tui-passthrough.md](codex-tui-passthrough.md).

---

## Quick Reference

```bash
# Start server
meridian chat --port 8765

# Create a chat
curl -s -X POST http://localhost:8765/chat | jq .
# {"chat_id":"c-abc...","state":"idle"}

# Send a prompt
curl -s -X POST http://localhost:8765/chat/c-abc/msg \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello"}'

# Stream events (after opening WebSocket, first messages replay history)
# Then receive:
# {"type":"turn.started","seq":1,...}
# {"type":"content.delta","seq":2,"payload":{"stream_kind":"assistant_text","text":"Hi!"}}
# {"type":"turn.completed","seq":3,...}

# Get state
curl -s http://localhost:8765/chat/c-abc/state

# Close
curl -s -X POST http://localhost:8765/chat/c-abc/close
```
