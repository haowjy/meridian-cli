# Smoke test: `meridian chat` local backend startup

Purpose: prove backend-only chat starts from the CLI with no browser or cloud
service, including nested/delegated executions used by smoke agents.

## Nested-session check

Nested launch should work for startup commands:

```bash
MERIDIAN_DEPTH=2 uv run meridian chat --headless --port 0
```

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
