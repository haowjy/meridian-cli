# Behavioral Spec — Nested Chat Launch Guard Removal

## EARS Notation

Requirements use EARS (Easy Approach to Requirements Syntax):
- **Ubiquitous**: `The <system> shall <behavior>`
- **Event-driven**: `When <event>, the <system> shall <behavior>`
- **State-driven**: `While <state>, the <system> shall <behavior>`
- **Unwanted behavior**: `If <condition>, then the <system> shall <behavior>`
- **Optional**: `Where <feature>, the <system> shall <behavior>`

---

## B1: Nested chat launch

**B1.1** When `meridian chat --headless` is invoked from a nested Meridian process (`MERIDIAN_DEPTH > 0`) with a valid `MERIDIAN_SPAWN_ID`, the system shall start a headless chat backend server on an auto-assigned port and bind to the requested host.

**B1.1a** If `MERIDIAN_DEPTH > 0` and `MERIDIAN_SPAWN_ID` is absent or empty, then the system shall fail with a targeted error: "nested chat launch requires MERIDIAN_SPAWN_ID for state isolation."

**B1.1b** If `meridian chat` is invoked from a nested process without `--headless` (and without `--dev`), the system shall start in normal mode (serving built frontend assets if available, falling back to headless). Browser auto-open (`--open`) shall be silently ignored when nested.

**B1.1c** If `meridian chat --dev` is invoked from a nested process, the system shall fail with a clear error (dev mode requires interactive terminal).

**B1.2** When a nested chat server starts, the system shall resolve chat policy (model, harness, agent, skills, approval) through the shared `resolve_launch_policy` path, identical to root-level chat.

**B1.3** When a nested chat server starts, the system shall use an isolated chat state directory scoped to the parent spawn ID (`MERIDIAN_SPAWN_ID`), so that the server does not discover, recover, or interfere with chats owned by other resolved chat scopes. Only one chat server may run per spawn scope; if a second launch attempts the same scope, it shall fail with a clear "another chat server is running in this spawn scope" error.

**B1.4** When a nested chat server starts, the system shall NOT write the user-level server discovery file (`chat-server.json`) at `get_user_home()`.

## B2: Server discovery isolation

**B2.1** While running from a root process (`MERIDIAN_DEPTH == 0`), the system shall write `chat-server.json` to `get_user_home()` for server discovery. (User-level singleton preserved for root — this is the existing behavior.)

**B2.2** While running from a nested process, `meridian chat` shall emit the server URL to stdout so callers can capture it programmatically. The system shall NOT write or modify the user-level discovery file.

**B2.3** When `meridian chat --headless` starts at any depth, the system shall print the bound URL in a parseable format on stdout (already true today).

## B3: Chat state isolation

**B3.1** The system shall scope the `chats/` state directory per resolved chat server scope, so that recovery on startup only scans chats created in that scope.

**B3.2** When a nested chat server creates a chat session, the chat ID, event log, policy snapshot, and event index shall be written under the scope-local chats directory.

**B3.3** When a nested chat server stops, it shall not leave orphaned state that causes a parent chat server's recovery to fail or adopt stale sessions.

## B4: SpawnManager isolation

**B4.1** When a nested chat server creates a `SpawnManager`, the runtime root used for spawn index tracking shall be isolated from the parent's runtime root.

**B4.2** The system shall not create spawn ID collisions between parent and nested chat server backend spawns (already true via UUID-based IDs: `chat-{uuid4()}`).

## B5: Management subcommands from nested

**B5.1** When `meridian chat ls`, `meridian chat show`, `meridian chat log`, or `meridian chat close` is invoked from a nested process with an explicit `--url`, the system shall execute the command against the specified server.

**B5.2** When a management subcommand is invoked from a nested process WITHOUT `--url`, the system shall fail with a clear error: "nested chat management commands require --url; implicit discovery is root-only."

**B5.3** While running from a root process, management subcommands shall continue to use the user-level discovery file (`get_user_home()/chat-server.json`) when `--url` is not provided.

## B6: Side-effect safety

**B6.1** While running from a nested process, the chat server shall NOT trigger root-only side effects (reaper, orphan reconciliation, global doctor maintenance).

**B6.2** The system shall not modify `MERIDIAN_DEPTH` in the environment to bypass the nested detection (per requirements non-goal).

## B7: Port allocation

**B7.1** When `--port 0` (default) is used, the system shall auto-assign an available ephemeral port regardless of execution depth.

**B7.2** When an explicit `--port N` is used and the port is occupied, the system shall fail with a clear bind error.

## B8: Process lifecycle

**B8.1** When a nested chat server process is terminated (SIGTERM/SIGINT), the system shall stop gracefully — draining pipelines and closing chat sessions — identical to root behavior.

**B8.2** If a nested chat server process crashes, the scoped state directory shall be recoverable by a subsequent chat server start that resolves to the **same scope** (same `MERIDIAN_SPAWN_ID` for nested, or root scope for root). Cross-scope recovery is not supported — orphaned nested scopes are inert residue until explicitly cleaned up.

## B9: Non-goals (negative requirements)

**B9.1** The system shall NOT support `meridian chat --dev` (with frontend) from nested execution. Dev mode requires interactive terminal and is not a smoke-test scenario.

**B9.2** The system shall NOT automatically open a browser from nested execution (silently ignore `--open` when nested).

---

## Verification strategy

| ID | Verification method | Notes |
|----|-------------------|-------|
| B1.1 | Smoke test | `MERIDIAN_DEPTH=2 MERIDIAN_SPAWN_ID=test1 uv run meridian chat --headless` starts and binds |
| B1.1a | Smoke test | `MERIDIAN_DEPTH=2` without `MERIDIAN_SPAWN_ID` → targeted error |
| B1.1b | Smoke test | Nested non-headless with built assets → serves; `--open` silently ignored |
| B1.1c | Smoke test | `--dev` from nested → clear error |
| B1.2 | Smoke test | Nested chat resolves model/harness from config, not defaults |
| B1.3 | Smoke test | Start two chat servers (different spawn IDs), verify chats don't cross-contaminate |
| B1.4 | Smoke test | Verify `get_user_home()/chat-server.json` unchanged after nested server start |
| B2.1-B2.3 | Smoke test | Verify user-level discovery file written only from root |
| B3.1-B3.3 | Unit test | Recovery scans only scope-local directory |
| B4.1-B4.2 | Design review | UUID uniqueness, runtime root scoping |
| B5.1-B5.3 | Smoke test | Management commands with/without --url at depth > 0 |
| B6.1 | Code review | Verify is_root_side_effect_process guards remain |
| B7.1-B7.2 | Smoke test | Port allocation at various depths |
| B8.1 | Smoke test | SIGTERM during nested headless → graceful shutdown |
| B8.2 | Smoke test | Crash nested → restart same scope → recovers; restart different scope → no cross-contamination |
| B9.1-B9.2 | Smoke test | `--dev` from nested → error; `--open` from nested → silently ignored |
