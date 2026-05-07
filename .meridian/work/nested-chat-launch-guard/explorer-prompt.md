# Explore: chat launch guard and isolation mechanisms

## Objective

Map all the technical details of the chat launch guard and the isolation mechanisms it was protecting. We need to understand what breaks if we simply remove the guard.

## Research questions

1. **Guard call sites**: `_require_root_process()` is called in `src/meridian/cli/chat_cmd.py` at lines 168, 210, 223, 246, 262 — that's the main `_chat()` function plus all management subcommands (`_chat_ls`, `_chat_show`, `_chat_log`, `_chat_close`). Confirm there are no other guards elsewhere.

2. **Server discovery file collision**: `_write_server_discovery()` writes `chat-server.json` to `runtime_root` (from `get_user_home()`). The management subcommands read this same file via `_server_discovery_path()`. If a nested spawn launches its own chat server, does it overwrite the parent's discovery file? The runtime root is per-user, not per-spawn.

3. **Chat state directory isolation**: `RuntimePaths.chats_dir` is `runtime_root / "chats"`. Chat IDs are UUID-based (`c-{uuid4().hex}`), so they won't collide. But does the recovery logic in `ChatRuntime.start()` scan ALL chats in the directory? Could a nested chat server recover and interfere with a parent's live chats?

4. **Port allocation**: `_find_free_port(host)` uses ephemeral port binding. This naturally avoids collision. But does `--port 0` (the default) work cleanly from nested execution? Any env vars that might conflict?

5. **SpawnManager instantiation**: The chat server creates its own `SpawnManager(runtime_root=runtime_root, project_root=project_root)`. If two chat servers share the same `runtime_root`, do their spawn managers conflict on the spawn index (`spawns.jsonl` and its flock)?

6. **Chat backend spawns**: When a chat session acquires a backend, it spawns a harness process via `ColdSpawnAcquisition`. The spawn ID uses `SpawnId(f"chat-{uuid4()}")`. Are these tracked in the shared spawn store? Could a nested chat server's backend spawns collide with the parent's?

7. **Process-level uvicorn**: The chat server runs `uvicorn.run(chat_app, host=host, port=actual_port)` which blocks the process. A nested spawn launching chat would have this process as the spawn's main process. Does this interact with spawn lifecycle tracking (heartbeat, status reporting)?

8. **Telemetry and logging**: Do chat server telemetry events use any depth-aware routing?

## Files to examine

Start with these, follow references as needed:
- `src/meridian/cli/chat_cmd.py` — the guard and server setup
- `src/meridian/lib/chat/runtime.py` — ChatRuntime recovery and registration
- `src/meridian/lib/chat/recovery.py` — recovery logic
- `src/meridian/lib/state/paths.py` — RuntimePaths.chats_dir
- `src/meridian/lib/state/user_paths.py` — get_user_home()
- `src/meridian/lib/streaming/spawn_manager.py` — SpawnManager shared state
- `src/meridian/lib/core/depth.py` — depth primitives
- `src/meridian/lib/chat/policy.py` — build_chat_backend_launch_plan child env

## Output

Write findings to `/home/jimyao/gitrepos/meridian-cli/.meridian/work/nested-chat-launch-guard/design/exploration-findings.md`. Structure as:
1. Guard call sites (comprehensive list)
2. Isolation risks (each risk with severity and evidence)
3. What the guard was actually protecting (distill from the evidence)
4. What already works fine without the guard
