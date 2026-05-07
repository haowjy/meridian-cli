# Chat Backend

`meridian chat` starts a local server that exposes agent conversations over REST
and WebSocket. It is the programmatic backend for browser-based UIs, custom
tooling, and any client that needs a structured event stream instead of a raw
terminal. Pass `--headless` for API-only mode with no frontend.

## Start the Server

```bash
meridian chat --headless                        # Claude, random port, API-only
meridian chat --headless -m codex               # codex alias; harness derived from model route
meridian chat --headless --harness codex        # explicit harness
meridian chat --headless --harness opencode     # OpenCode
meridian chat --headless -m gpt-4o              # explicit model
meridian chat --headless -a reviewer            # reviewer agent profile
meridian chat --headless -a reviewer -m gptmini # CLI model wins over profile default
meridian chat --headless --skills md-validation # add skills
meridian chat --headless --approval auto        # approval mode
meridian chat --headless --port 8765            # fixed port
meridian chat --headless --port 8765 --host 0.0.0.0  # listen on all interfaces
meridian chat                                   # serve frontend when assets present, else headless
meridian chat --frontend-dist /path/to/dist     # explicit built-asset path
meridian chat --dev                             # dev mode: Vite subprocess + verbose logging
meridian chat --dev --frontend-root ../meridian-web  # explicit source checkout for dev mode
meridian chat --dev --open                      # open browser after server starts
meridian chat --dev --tailscale                 # share dev server on Tailscale network
meridian chat --dev --funnel                    # expose dev UI publicly via Tailscale Funnel
meridian chat --dev --no-portless               # skip portless; use raw Vite instead
meridian chat --dev --portless-force            # take over an occupied portless dev route
```

On startup, the server prints its URL and blocks. With frontend assets:

```
Chat UI: http://127.0.0.1:52341
```

Without frontend assets (headless fallback or `--headless`):

```
Chat backend: http://127.0.0.1:52341
```

### Flags

**Launch policy flags** — resolved before the server starts. A harness/model conflict
fails before port binding, `chat-server.json` is written, or any frontend assets mount.

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `-m/--model NAME` | configured default | Model id or alias. Resolved through the shared model catalog; harness derived from the resolved route when `--harness` is omitted. |
| `--harness NAME` | derived from model | Harness: `claude`, `codex`, `opencode`. Must be compatible with the resolved model; conflict fails before server startup. |
| `-a/--agent AGENT` | config default | Agent profile. Loads the same `.mars/agents/*.md` format used by primary/spawn. Profile fields (model, harness, approval, tools, skills, etc.) apply through shared policy semantics; CLI flags override at higher precedence. Missing explicit `-a` fails before startup; missing configured default emits a warning and continues. |
| `-s/--skills SKILL` | — | Additional skills (repeatable). Merged with profile skills — profile skills first, CLI skills appended, duplicates removed by first occurrence. |
| `--approval MODE` | config/profile default | Tool approval mode: `default`, `confirm`, `auto`, `yolo`. Resolved through the shared permission pipeline. |
| `--yolo` | — | Shorthand for `--approval yolo`. |
| `--effort LEVEL` | profile/harness default | Effort / reasoning level, if supported by the harness. |
| `--sandbox MODE` | profile default | Sandbox mode value, if supported by the harness. |
| `--autocompact PCT` | profile/harness default | Enable autocompact at N% context use (integer percentage), when supported by the resolved harness. Current chat backend applies launch-time autocompact only for Claude. |

`--timeout` is not supported for `meridian chat`. Chat has server-lifetime, per-turn,
idle, and recovery timeout dimensions that require separate design.

**Transport and frontend flags** — control server binding and frontend serving only; not part of policy resolution.

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--port PORT` | `0` (auto) | Port to bind; `0` picks a free port |
| `--host HOST` | `127.0.0.1` | Interface to bind |
| `--headless` | off | API-only mode; no frontend is started regardless of available assets. |
| `--frontend-dist PATH` | — | Path to a pre-built frontend assets directory (`index.html` + `assets/`). Errors if specified path has no valid assets. |
| `--dev` | off | Dev mode: start a Vite subprocess against the `meridian-web` source checkout, with verbose logging. Cannot be combined with `--headless` or `--frontend-dist`. |
| `--frontend-root PATH` | auto-discovered | Path to the `meridian-web` source checkout used in `--dev` mode. Auto-discovered as `../meridian-web` or `$MERIDIAN_DEV_FRONTEND_ROOT`. Only valid with `--dev`. |
| `--open` | off | Open a browser tab after the server starts. Ignored in `--headless` mode. |
| `--tailscale` | off | Share the dev server on the Tailscale network via portless. Requires `--dev` and a portless installation. |
| `--funnel` | off | Expose the dev UI publicly via Tailscale Funnel. Implies `--tailscale`. Requires `--dev` and portless. |
| `--no-portless` | off | Disable portless in dev mode and use raw Vite directly. Only valid with `--dev`. Cannot be combined with `--tailscale` or `--funnel`. |
| `--portless-force` | off | Take over an occupied portless dev route at startup. Only valid with effective portless dev mode (`--dev` without `--no-portless`). |

**Non-headless default behavior**: when `--headless` is not set and `--dev` is not set, the server attempts to serve built frontend assets. Asset resolution order: explicit `--frontend-dist` → packaged assets (`meridian.web_dist`) → `../meridian-web/dist` sibling path. If valid assets are found the server prints `Chat UI: <url>` and mounts the frontend. If no assets are found the server prints a notice and falls back to headless mode.

**Invalid flag combinations** — each exits before binding a port:

| Combination | Error |
| ----------- | ----- |
| `--headless --dev` | `--dev cannot be combined with --headless` |
| `--headless --frontend-dist PATH` | accepted — headless wins; frontend assets are unused |
| `--dev --frontend-dist PATH` | `--frontend-dist cannot be combined with --dev` |
| `--frontend-root PATH` (without `--dev`) | `--frontend-root is only valid with --dev` |
| `--no-portless` (without `--dev`, with or without `--headless`) | `--no-portless is only valid with --dev` |
| `--tailscale` or `--funnel` (without `--dev`, with or without `--headless`) | `--tailscale and --funnel are only valid with --dev` |
| `--tailscale --funnel` | `--tailscale and --funnel cannot be combined` |
| `--no-portless --tailscale` or `--no-portless --funnel` | `--no-portless cannot be combined with --tailscale or --funnel` |
| `--portless-force` (without effective portless dev mode) | `--portless-force is only valid with portless dev mode` |
| `--headless --portless-force` | `dev frontend flags cannot be combined with --headless` |
| `--yolo --approval MODE` | `Cannot use --yolo with --approval` |

Startup writes `~/.meridian/chat-server.json` with the current base URL so management
commands can find the running server. Pass `--url` to any management command to
override discovery.

### Usage context

`meridian chat` and all its subcommands (`ls`, `show`, `log`, `close`) are for
**root-process use only**. Running them inside a spawn (where `MERIDIAN_DEPTH > 0`)
exits immediately with a clear error. Start the chat server from your terminal or
a top-level process, not from within an agent.

---

## Launch Policy

`meridian chat` participates in the same shared launch policy resolution used by
`meridian spawn` and the primary session. Policy is resolved once at startup — a
conflict fails before the server binds a port, writes `chat-server.json`, or mounts
any frontend assets.

### Model and harness

When `-m <alias>` is supplied, the alias is resolved through the shared model catalog
before any harness launch spec is built. When no `--harness` is given, the harness is
derived from the resolved model route. An explicit `--harness` that conflicts with the
resolved model route fails before startup.

| Command | Resolved behavior |
| ------- | ----------------- |
| `meridian chat -m codex` | Model `gpt-5.3-codex`; harness `codex` derived from catalog. |
| `meridian chat --harness codex -m codex` | Model `gpt-5.3-codex`; harness `codex`; no raw alias reaches provider. |
| `meridian chat --harness claude -m gptmini` | Fails: `gptmini` routes to Codex, incompatible with `claude`. |

When no model is supplied, chat uses the configured default policy (chat, primary, or
global default) rather than forcing a catalog lookup.

### Agent profiles

`-a AGENT` loads the same `.mars/agents/*.md` profile format used by primary and spawn.
All profile fields apply through the shared policy engine: model, harness, approval,
sandbox, effort, autocompact, tools, disallowed tools, MCP tools, skills, model
policies, and fanout.

Precedence follows the shared launch rule — per field, independently:

```
CLI flag > environment variable > agent overlay model policy > agent overlay default
  > profile model policy > profile default > config default > alias default > harness fallback
```

A CLI model override (`-m`) derives/replaces the harness even if a lower-precedence
profile supplied one. Profile values do not win over CLI overrides, even indirectly.

If an explicitly requested agent (`-a AGENT`) is missing, chat fails before startup.
If a configured default agent is missing, chat emits the shared missing-agent warning
and continues without a profile.

### Skills

`--skills` (repeatable) adds skills to those provided by the agent profile. Merge
order: profile skills first, then CLI-supplied skills, deduped by first occurrence.
The harness/model-specific variant is selected the same way primary/spawn would select
it for the resolved harness and model.

If a skill is unavailable, chat emits the shared missing-skill warning and continues
with available skills (unless the shared resolver defines the case as fatal).

On first acquisition, resolved agent and skill prompt content is projected through
shared prompt projection helpers, not ad hoc chat-only instruction strings.

### Approval and tool policy

`--approval` is resolved through the shared policy compiler using the same precedence
as other launch surfaces:

```
--approval flag > MERIDIAN_APPROVAL env > agent overlay model policy > agent overlay default
  > profile model policy > profile default > config default > alias default > harness fallback
```

Profile tools, disallowed tools, and MCP tools are passed to harness projection
through the typed run-input path used by primary and spawn. `--approval` cannot be
silently replaced by a no-op resolver after policy resolution completes.

### Policy snapshot and restart

When a new chat is created, the server persists a serializable policy snapshot for
that chat before first backend acquisition. First acquisition uses that snapshot —
not live CLI flags or config at acquisition time.

After a server restart, reacquisition for an existing chat uses the chat's persisted
policy snapshot, including immutable agent and skill prompt inputs captured at chat
creation. Changed agent or skill source files are not silently reloaded.

The `chat.configured` event reports the canonical model and resolved harness from the
policy snapshot, not raw aliases.

### Separation from spawn

`meridian chat` is a chat runtime. It does not create spawn lifecycle rows, report
artifacts, execution budgets, or primary continuation/fork state. Policy sharing with
spawn is at the resolution layer only; chat management subcommands (`ls`, `show`,
`log`, `close`) are runtime-management commands and do not accept launch-policy flags.

---


## Management CLI

These commands connect to a running chat server. They are runtime-management only
and do not accept launch-policy flags (`-m`, `--harness`, `-a`, `--skills`,
`--approval`, etc.). By default they read the server URL from
`~/.meridian/chat-server.json`; use `--url http://host:port` to target a specific
server. Root-level harness selectors are also rejected for management commands
(`meridian --harness codex chat ls`, `meridian codex chat ls`).

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

A chat is a persistent conversation backed by one agent process. The launch policy
(model, harness, agent, skills, approval) is resolved once at server startup and
persisted as an immutable snapshot per chat at creation time. Backend is acquired
on the first prompt, not on creation, using the chat's persisted snapshot.

```
POST /chat              →  reserve chat_id, persist policy snapshot (no agent starts yet)
POST /chat/{id}/msg     →  cold-start the harness from the snapshot, deliver first prompt
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

Body: empty or `{}`. Launch policy (model, harness, agent, skills, approval) is
resolved once at server startup and snapshotted per chat at creation time. There
are no per-chat model or harness overrides — all policy comes from the flags
passed when the server was started.

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
| `swap_model` | `model` | **Not supported.** Recognized by the wire protocol but rejected — model is fixed by the server's launch-policy snapshot and cannot be changed per-turn. |
| `swap_effort` | `effort` | **Not supported.** Recognized by the wire protocol but rejected — no per-turn policy switching. |

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
| Claude (`claude`) | No | No | Launch-time permissions only. Parses `--output-format stream-json`; live assistant/tool events normalize into canonical `turn.*`, `content.delta`, and `item.*` chat events. |
| Codex (`codex`) | Yes | No | Connects to Codex app-server via WebSocket; live assistant/tool events normalize into canonical `turn.*`, `content.delta`, and `item.*` chat events. |
| OpenCode (`opencode`) | No | No | Connects to OpenCode HTTP SSE API; live assistant/tool events normalize into canonical `turn.*`, `content.delta`, and `item.*` chat events. |

Model is fixed at the server launch policy snapshot. `swap_model` and `swap_effort` WebSocket commands are recognized but rejected.

For Codex managed primary session behavior, see [codex-tui-passthrough.md](codex-tui-passthrough.md).

---

## Quick Reference

```bash
# Start server (backend-only / API mode)
meridian chat --headless --port 8765

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
