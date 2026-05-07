# Exploration findings — nested chat launch guard

## 1. Guard call sites

`_require_root_process()` is defined in `src/meridian/cli/chat_cmd.py:65-77`.

It is called in exactly five chat entry points:

- `src/meridian/cli/chat_cmd.py:168` — `_chat()`
- `src/meridian/cli/chat_cmd.py:210` — `_chat_ls()`
- `src/meridian/cli/chat_cmd.py:223` — `_chat_show()`
- `src/meridian/cli/chat_cmd.py:246` — `_chat_log()`
- `src/meridian/cli/chat_cmd.py:262` — `_chat_close()`

I did not find any other `_require_root_process()` call sites outside `chat_cmd.py`.

There are other depth-sensitive checks elsewhere, but they are separate gates, not chat guards:

- `src/meridian/cli/main.py:178-180` — agent-mode detection
- `src/meridian/lib/config/project_root.py:67-74` — nested-exec warning around user config lookup
- `src/meridian/cli/startup/help.py:104-106` — help rendering behavior in nested execution

## 2. Isolation risks

### High — discovery file clobbering

`run_chat_server()` always uses `runtime_root = get_user_home()` (`src/meridian/cli/chat_cmd.py:353-355`), and `_write_server_discovery()` writes `chat-server.json` directly under that root (`src/meridian/cli/chat_cmd.py:570-577`). The management commands read the same file via `_server_discovery_path()` / `_resolve_server_url()` (`src/meridian/cli/chat_cmd.py:580-597`).

If a nested Meridian process launches its own chat server, it will write the same per-user discovery file and overwrite the parent server pointer. Management commands without `--url` will then talk to whichever server wrote last.

### High — recovery can adopt and mutate parent chats

`ChatRuntime.start()` performs a one-time `recover_all(...)` on startup (`src/meridian/lib/chat/runtime.py:118-163`). `recover_all()` scans every `*/history.jsonl` under `paths.chats_dir` (`src/meridian/lib/chat/recovery.py:24-82`), rebuilds event indexes, recreates live entries for any chat that has not exited, and appends a `runtime.error` if the last state looks active after restart.

Because `RuntimePaths.chats_dir` is `runtime_root / "chats"` (`src/meridian/lib/state/paths.py:92-111`) and `runtime_root` is shared per user in chat startup, a nested chat server would scan the same global chat tree as the parent. That means the nested server can:

- recover a parent’s live chats into its own in-memory registry,
- start pipelines for those chats,
- and write recovery/error events back into the same history files.

That is the strongest isolation risk in the current code.

### Medium — shared spawn namespace

Chat startup wires a `SpawnManager` with the same `runtime_root` (`src/meridian/cli/chat_cmd.py:353-361`, `src/meridian/cli/chat_cmd.py:694-715`). `SpawnManager` stores spawn state under `runtime_root/spawns` and uses the shared `spawns.flock` / history paths for coordination (`src/meridian/lib/streaming/spawn_manager.py:198-273, 743-775`; `src/meridian/lib/state/paths.py:69-90, 521-532`).

The chat backend launch plan assigns explicit random spawn IDs of the form `chat-{uuid4()}` (`src/meridian/cli/chat_cmd.py:737-755`), so deterministic collision with the parent is unlikely. But both chat servers still write into the same per-user spawn store, so they are sharing one mutable namespace rather than isolated per-server state.

### Low to medium — forced `PORT` env can still collide

`run_chat_server()` chooses `port` first, then falls back to `PORT` from the environment, then to `_find_free_port(host)` (`src/meridian/cli/chat_cmd.py:363-365, 564-567`).

So `--port 0` is clean by default, but any inherited `PORT` value will override ephemeral binding. That is not a depth bug; it is just an external override that can make two servers land on the same port.

### Low — the chat server process is blocking, but not depth-aware

`uvicorn.run(chat_app, host=host, port=actual_port)` blocks the process (`src/meridian/cli/chat_cmd.py:473-475`). The code here does not register the chat server process itself in the spawn store; only the backend harness children are tracked through `SpawnManager` and `ColdSpawnAcquisition` (`src/meridian/lib/chat/backend_acquisition.py:110-156`).

That means nested chat is a long-lived delegated process, but not one that the chat runtime specially treats as a tracked spawn lifecycle entity. I did not find evidence of a direct lifecycle corruption bug here; the main issue is operational, not structural.

### Low — telemetry/logging are not depth-routed

Chat telemetry calls are keyed by `chat_id` / `command_id`, not by `MERIDIAN_DEPTH` (`src/meridian/lib/chat/runtime.py:173-208`, `src/meridian/lib/chat/server.py:394-439`, `src/meridian/lib/chat/command_handler.py:198-223`). There is no depth-aware routing layer in this chat code.

## 3. What the guard was actually protecting

The guard is a coarse "root-only user-home mutation" firewall.

It is protecting against nested delegated execution touching shared per-user chat state in ways that are hard to reason about:

1. overwriting the single `chat-server.json` discovery pointer,
2. recovering and restarting all chats visible under the shared runtime root,
3. sharing the same per-user spawn store and heartbeat paths,
4. and turning a delegated run into a blocking long-lived server that owns root chat state.

What it is **not** protecting:

- chat ID uniqueness,
- ephemeral port allocation,
- or telemetry/log partitioning.

Those are already handled by UUIDs, socket binding, or depth-agnostic code.

## 4. What already works fine without the guard

- Chat IDs are UUID-based (`c-{uuid4().hex}`), so chat history paths do not collide (`src/meridian/lib/chat/runtime.py:210-218`).
- Backend spawn IDs are also UUID-based (`chat-{uuid4()}`), so explicit spawn IDs are effectively unique even when multiple servers exist (`src/meridian/cli/chat_cmd.py:737-755`; `src/meridian/lib/state/spawn_store.py:233-316`).
- Default port selection is already safe in the common case: `port=0` falls back to ephemeral binding, and `_find_free_port()` binds the requested host directly (`src/meridian/cli/chat_cmd.py:363-365, 564-567`).
- `_write_server_discovery()` uses atomic tmp+rename writes (`src/meridian/cli/chat_cmd.py:570-577`), so the discovery pointer itself is crash-safe even though it is shared.
- Chat telemetry and command logging do not depend on depth, so they would keep working the same way.
- Management commands already accept `--url`, so callers can bypass the discovery file when they need to point at a specific server.

