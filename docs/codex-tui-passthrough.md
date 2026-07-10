# Codex TUI Passthrough

Meridian supports a managed primary Codex path instead of treating Codex as a pure black-box TUI.

## What It Does

For `meridian codex`, Meridian now:

1. Starts Codex `app-server`
2. Connects a managed observer
3. Starts or resumes the Codex thread with split instruction channels
4. Attaches the real Codex TUI with `codex resume <thread-id> --remote <ws-url>`

This gives Meridian a real Codex thread ID, managed startup telemetry, and hidden instruction delivery for the role/system tiers.

## Workspace Roots

Configured `[workspace.*]` roots reach the remote TUI, not just the app-server.

When Meridian builds the `codex resume --remote` attach command it appends `--add-dir <path>` for every entry in `projected_roots`:

```
codex resume <session-id> --remote ws://127.0.0.1:<port> \
  --add-dir /abs/path/sibling-repo \
  --add-dir /abs/path/other-repo
```

This matters because Codex's remote-TUI mode sends per-turn sandbox policy constructed from the TUI's local permission profile. Without the `--add-dir` flags the remote TUI's per-turn overrides would narrow writable scope below what the app-server was originally configured with, causing approval prompts or write failures for paths that should be freely writable.

Roots are deduplicated and resolved to absolute paths before projection. Paths that do not exist on disk are skipped and surfaced as findings: committed entries emit `workspace_missing_root`, and missing local-only entries emit `workspace_local_missing_root`.

## Approval Routing

Managed primary sessions surface approval requests instead of auto-accepting or rejecting them.

In the managed architecture Meridian is the websocket client connected to the Codex app-server. When Codex issues a `*/requestApproval` or `item/tool/requestUserInput` JSON-RPC server request, it goes to Meridian — not to the TUI. Meridian dispatches these through an interactive handler (`ManagedPrimaryRequestHandler`) that records them as `request/opened` events in the spawn's harness history. A controller (CLI or API) can then answer via `respond_request()` or `respond_user_input()`.

Key properties:

- **Requests are surfaced, not auto-accepted.** Pending requests stay open until a controller responds or Codex times out.
- **Subagent spawns are unaffected.** Spawn/subagent sessions continue using `AutoAcceptHandler` (auto-approve everything). Approval events from a spawn do not propagate to the parent session.
- **Unsupported server request methods** (anything other than approval and user-input variants) still return a transport-level error.

## Instruction Routing

Codex managed primary uses these channels:

- `baseInstructions`: agent/profile or role instructions
- `developerInstructions`: Meridian runtime instructions, skills, inventory, and reporting guidance
- `turn/start input`: the visible user turn

That keeps Meridian's system content out of the visible TUI prompt on managed paths.

## Fresh Session Bootstrap

Fresh Codex threads are not immediately attachable after `thread/start`. Codex needs at least one rollout materialized before `codex resume --remote ...` can attach.

Meridian handles that by sending a minimal bootstrap turn, then waiting until:

1. the Codex rollout file contains a matching `session_meta` entry for the project cwd;
2. the observer receives `turn/completed` for that bootstrap turn.

`Meridian started`

This bootstrap is intentionally small and deterministic. Meridian does **not** temporarily override Codex model/reasoning defaults for the bootstrap turn. The session should preserve Codex's own default or last-used settings.

## Why Fresh Start Feels Slower

Fresh `meridian codex` is slower than a black-box TUI launch because it does real managed work before the TUI appears:

- start `app-server`
- connect the observer
- create the thread
- run the bootstrap turn
- attach the TUI

Meridian shows compact startup telemetry for those phases so the delay is
legible:

- `Starting Codex app-server...`
- `Connecting managed observer...`
- `Creating fresh Codex thread...`
- `Materializing rollout...`
- `Attaching Codex TUI...`

## Attachability Gate

The important condition is:

`thread is attachable by the Codex TUI`

Rollout materialization makes the thread technically resumable. Meridian also waits
for the bootstrap turn to complete before handing the websocket endpoint to the TUI.
Attaching during the bootstrap turn can strand the remote TUI in a stale `Working`
state after the observer connection is displaced, which also prevents normal idle
Ctrl+C exit behavior.

If Codex exposes an atomic "completed turn handoff" signal, Meridian should prefer it
over coordinating the rollout and completion signals separately. It should not improve
startup time by:

- changing bootstrap wording
- lowering reasoning effort temporarily
- mutating user-visible Codex defaults

## Failure Behavior

Codex primary is managed-only. If managed startup fails, Meridian fails loudly instead of silently falling back to black-box Codex. This is deliberate: hidden instruction delivery and managed session tracking are the point of the command.

OpenCode behavior is different:

- primary resume uses managed attach
- other primary modes may still use black-box paths

## Cancellation and Process Cleanup

`meridian spawn cancel ID` on a managed primary uses sequenced teardown:

1. **Terminate launcher** — the Meridian launcher/wrapper process tree is terminated first.
2. **Pause** — a brief pause gives any harness-driven session shutdown time to propagate.
3. **Terminate backend and TUI** — if the `app-server` or TUI processes are still running after the pause, they are terminated too.

This sequence gives Codex a chance to exit cleanly from the launcher side before Meridian reaches in to terminate the backend directly.

### Session lease preservation

The `app-server` backend for a managed primary is associated with a **session lease**. While the lease is active — meaning a session is still live — the backend scope is skipped during passive spawn cleanup (e.g., the orphan reaper). This prevents Meridian from killing a backend that is still in use.

When the session lease expires, or when `meridian spawn cancel` is called explicitly, the backend scope is reclaimed and the process is terminated.

### Process tree termination

Cleanup terminates full process trees, not just the root PID. If the launcher or backend spawned child processes (tool subprocesses, workers), those are also terminated as part of the same cleanup operation.

## Related Files

- [codex_ws.py](../src/meridian/lib/harness/connections/codex_ws.py)
- [primary_attach.py](../src/meridian/lib/launch/process/primary_attach.py)
- [runner.py](../src/meridian/lib/launch/process/runner.py)
