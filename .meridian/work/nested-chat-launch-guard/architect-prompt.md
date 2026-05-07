# Architecture: nested-safe chat launch

## Problem

`_require_root_process()` in `src/meridian/cli/chat_cmd.py` blocks all chat commands when `MERIDIAN_DEPTH > 0`. This prevents delegated smoke testers from running real `meridian chat --headless` E2E tests.

## What the guard protects (from code analysis)

The guard was added as a blanket safety measure. The real risks it covers:

1. **Server discovery file collision**: `chat-server.json` written to `get_user_home()` — shared across all processes for the same user. A nested chat server overwrites the parent's discovery file.
2. **Chat state recovery cross-talk**: `ChatRuntime.start()` recovers ALL chats from `runtime_root/chats/`. A nested server would recover and try to manage the parent's live chats.
3. **SpawnManager shared state**: Both parent and nested chat servers use `SpawnManager` against the same `runtime_root`, sharing `spawns.jsonl` and its flock.
4. **Root-only side effects**: `is_root_side_effect_process()` guards reaper and orphan reconciliation. These should NOT run from nested chat servers.

## What doesn't need the guard

1. **Port allocation**: `_find_free_port()` auto-assigns ephemeral ports — no collision.
2. **Chat IDs**: UUID-based — no collision.
3. **Policy resolution**: Uses shared `resolve_launch_policy()` — already works from any depth.
4. **Management subcommands** (`chat ls`, `chat show`, `chat log`, `chat close`): These are HTTP clients that talk to a running server. They just need the right `--url`. The guard on these is overly broad — they should work from any depth if pointed at the right server.

## Design task

Produce competing architectural options for making chat launch safe from nested execution. Consider at minimum:

### Option A: Simple guard removal + scoped server discovery
- Remove `_require_root_process()` from `_chat()`
- For management subcommands, remove the guard but require `--url` when nested (no discovery file lookup from nested)
- Server discovery file: either scope to include depth/spawn-id in the filename, or skip writing it from nested launches
- Chat recovery: filter recovered chats by some ownership marker so nested servers don't adopt parent's chats

### Option B: Isolated runtime root for nested chat servers
- Derive a separate runtime root for nested chat servers (e.g. under the spawn's artifacts directory)
- This naturally isolates: chats dir, spawn manager state, server discovery
- But adds complexity to state management

### Option C: Guard relaxation with --allow-nested flag
- Keep the guard but add an escape hatch flag
- Least invasive but adds CLI surface and doesn't solve the isolation problem

Evaluate tradeoffs: reversibility, complexity, isolation quality, testability.

## Constraints
- Crash-only/file-authority design must be preserved
- Chat must continue using shared policy resolution (not chat-specific)
- Headless mode is the primary nested use case
- Management subcommands should work from nested if given explicit --url
- No session bleed between parent and nested chat servers

## Output

Write the architecture document to `/home/jimyao/gitrepos/meridian-cli/.meridian/work/nested-chat-launch-guard/design/architecture/target-architecture.md`. Include:
1. Option comparison table
2. Recommended option with justification
3. Component-level changes
4. Risk assessment
5. Migration path (is this a breaking change?)
