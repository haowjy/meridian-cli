# Smoke test: `meridian chat` local backend startup

Purpose: prove backend-only chat starts from the CLI with no browser or cloud
service, and document the root-process requirement for live chat smoke runs.

## Root-session requirement

Run this guide from a top-level terminal, not from a delegated Meridian spawn.

In nested/delegated execution, `meridian chat ...` is expected to fail
immediately with:

```text
error: meridian chat requires a root Meridian process. Chat commands cannot run inside a nested spawn or delegated execution.
```

If you hit that guard, switch to a root shell for the live smoke run. For
shared-policy regression coverage tied to this rollout, also run
`tests/smoke/chat/test_shared_policy_surface.md`.

## Startup

```bash
uv run meridian chat --headless --port 0
```

Expected:

- stdout prints `Chat backend: http://127.0.0.1:<port>`.
- Process keeps running until interrupted.
- Server exposes REST routes such as `POST /chat` and WebSocket route `/ws/chat/{chat_id}`.
- No browser UI opens and no hosted service is required.

## Harness matrix

Run one backend-only chat flow for each supported harness:

```bash
uv run meridian chat --headless --harness claude --port 0
uv run meridian chat --headless --harness codex --port 0
uv run meridian chat --headless --harness opencode --port 0
```

For each run, create a chat through `POST /chat`, send the first prompt through
`POST /chat/{chat_id}/msg`, observe events over `/ws/chat/{chat_id}`, then stop
the server with Ctrl-C.
