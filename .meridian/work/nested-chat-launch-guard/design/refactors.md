# Preparatory Refactors — Nested Chat Launch Guard

## Assessment

No preparatory refactors are needed. The existing code is well-structured for this change:

1. `_require_root_process()` is isolated in a single function called from exactly 5 sites — clean removal.
2. `run_chat_server()` already receives `runtime_root` via `get_user_home()` at a single assignment point (line 353) — easy to swap.
3. `_write_server_discovery()` already receives `runtime_root` as a parameter — conditional write is straightforward.
4. `RuntimePaths.from_root_dir()` already parameterizes all paths from a root — different root = different paths.
5. The management subcommands already accept `--url` — adding a nested-requires-url check is additive.

## Structural health

The chat module's structure is clean. The boundaries are:
- `chat_cmd.py` — CLI surface, wiring, guard
- `runtime.py` — in-memory chat registry, lifecycle
- `recovery.py` — disk-to-memory hydration
- `server.py` — HTTP transport
- `policy.py` — launch plan preparation
- `backend_acquisition.py` — backend spawn wiring

Each module has a single reason to change and clear input boundaries. The scope resolver is a natural addition alongside this structure.

## No pre-existing debt blocks this work

The `get_user_home()` usage in `run_chat_server()` is not technical debt — it was the correct design when chat was root-only. The change is an intentional scope expansion, not a debt cleanup.
