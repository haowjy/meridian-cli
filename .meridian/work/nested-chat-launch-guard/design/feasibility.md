# Feasibility Assessment — Nested Chat Launch Guard

## Verdict: feasible, moderate complexity

This is not a trivial guard removal — the guard was protecting against real state isolation issues. But the fix is architecturally clean: scoped runtime roots for nested chat servers, with root flows unchanged.

## Complexity estimate

- **New code**: ~80-120 lines for the chat scope resolver module
- **Modified code**: ~30-50 lines in `chat_cmd.py` (replace guard calls with scope-aware behavior)
- **Test updates**: Moderate — existing chat CLI tests need scope awareness, new nested smoke tests
- **Risk**: Low-to-medium. Root chat behavior is unchanged. Nested chat is new behavior (previously blocked).

## Key feasibility evidence

### 1. Recovery isolation is correct by construction
The `recover_all()` function in `recovery.py` scans `paths.chats_dir` which is derived from `RuntimePaths.from_root_dir(runtime_root)`. Once nested chat servers use a different `runtime_root`, recovery naturally scopes to the correct chats. No changes needed in `recovery.py` itself.

### 2. SpawnManager isolation is correct by construction
Same pattern — `SpawnManager(runtime_root=..., project_root=...)` scopes all spawn state under the given root. Different root = isolated spawn namespace.

### 3. Port allocation already works
`_find_free_port(host)` uses ephemeral socket binding — no depth awareness needed.

### 4. Chat IDs already don't collide
UUID-based generation (`c-{uuid4().hex}`) across all servers.

### 5. Policy resolution already works from any depth
`resolve_launch_policy()` is depth-agnostic by design (shared launch policy work is done).

### 6. Discovery file is a simple conditional
Write to `get_user_home()` only when `depth == 0`. Straightforward conditional.

## What needs probing before implementation

### Already answered
- Recovery cross-talk: confirmed by explorer — `recover_all` scans all `*/history.jsonl` under chats_dir
- Discovery collision: confirmed — single `chat-server.json` in `get_user_home()`
- SpawnManager sharing: confirmed — both servers would use same runtime root

### No runtime probes needed
The architecture relies on existing, well-understood mechanisms (directory scoping, `RuntimePaths.from_root_dir()`, `is_nested_meridian_process()`). No external integrations or undocumented behaviors to probe.

## Prerequisites

None. The shared policy resolution work (which this depends on) is already merged.

## Residue / follow-up work

- Nested chat server scope directories accumulate. Cleanup is deferred (crash-only design prefers durable residue over implicit cleanup).
- Future: `meridian doctor` could learn to scan and clean stale nested chat scopes.
