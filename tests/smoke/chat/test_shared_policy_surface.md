# Chat shared-policy surface smoke

Purpose: verify that `meridian chat` uses the shared launch-policy pipeline for
model/harness resolution, agent profiles, skills, approval, restart
reacquisition, and management-command boundaries.

## Execution context

This guide can run from a root shell or delegated spawn. Nested launch smoke:

```bash
MERIDIAN_DEPTH=2 uv run meridian chat --headless --port 8765
```

If you cannot run live startup in the current environment, use these automated
fallback references instead of treating the smoke as passed:

- `tests/integration/chat/test_chat_cli.py::test_chat_policy_resolution_fails_before_runtime_configure_or_discovery_write`
- `tests/integration/chat/test_chat_cli.py::test_chat_policy_snapshot_resolves_alias_to_canonical_model`
- `tests/integration/chat/test_chat_cli.py::test_chat_policy_snapshot_rejects_incompatible_explicit_harness`
- `tests/integration/chat/test_chat_cli.py::test_chat_policy_snapshot_with_agent_and_cli_overrides_feeds_launch_plan`
- `tests/integration/chat/test_chat_cli.py::test_chat_management_subcommands_reject_launch_policy_flags`
- `tests/unit/chat/test_policy_runtime.py::test_chat_runtime_persists_policy_snapshot_on_create_before_first_acquire`
- `tests/unit/chat/test_policy_runtime.py::test_chat_runtime_recovery_uses_persisted_snapshot_not_new_runtime_default`

## Disposable setup

Use a disposable runtime root so you can inspect on-disk state safely:

```bash
export MERIDIAN_HOME="$(mktemp -d)"
export CHAT_URL="http://127.0.0.1:8765"
```

The per-chat policy snapshot should appear at:

```text
$MERIDIAN_HOME/chats/<chat_id>/policy.json
```

## 1. Alias success: `--harness codex -m codex`

Terminal A:

```bash
uv run meridian chat --headless --port 8765 --harness codex -m codex
```

Expected before any prompt:

- Startup succeeds.
- stdout prints `Chat backend: http://127.0.0.1:8765`.
- `"$MERIDIAN_HOME/chat-server.json"` exists.

Terminal B:

```bash
curl -s -X POST "$CHAT_URL/chat" -H 'content-type: application/json' -d '{}'
```

Record the returned `chat_id`, then send the first prompt:

```bash
curl -s -X POST "$CHAT_URL/chat/<chat_id>/msg" \
  -H 'content-type: application/json' \
  -d '{"text":"Reply with exactly OK"}'
```

Verify `"$MERIDIAN_HOME/chats/<chat_id>/policy.json"` shows:

- `"requested_model_token": "codex"`
- `"selected_model_token": "codex"`
- `"canonical_model_id": "gpt-5.3-codex"`
- `"harness": "codex"`

Then verify the configured event reports the canonical model, not the raw alias:

```bash
uv run meridian chat log <chat_id> --last 20
```

Expected:

- A `chat.configured` event is present after first acquisition.
- Its payload reports model `gpt-5.3-codex` and harness `codex`.
- Do **not** accept a `chat.configured` payload that still reports model
  `codex`.

Stop the server with `Ctrl-C` before moving on.

## 2. Harness conflict fails before side effects

Use a fresh runtime root for this section:

```bash
export MERIDIAN_HOME="$(mktemp -d)"
uv run meridian chat --headless --port 8765 --harness claude -m gptmini
```

Expected:

- Command exits non-zero.
- Error mentions the explicit harness is incompatible with model `gptmini`
  because that model routes to Codex.
- stdout/stderr does **not** print `Chat backend:` or `Chat UI:`.
- `"$MERIDIAN_HOME/chat-server.json"` is **not** created.
- No frontend/dev startup work begins.

## 3. Agent + skills + approval: `-a reviewer --skills md-validation --approval auto`

Use a fresh runtime root:

```bash
export MERIDIAN_HOME="$(mktemp -d)"
uv run meridian chat --headless --port 8765 \
  -a reviewer \
  --skills md-validation \
  --approval auto
```

Create a chat and trigger first acquisition as in section 1.

Verify `"$MERIDIAN_HOME/chats/<chat_id>/policy.json"` shows:

- `"agent_name": "reviewer"`
- `"approval": "auto"`
- `skills` contains the reviewer profile skills first, then appends
  `md-validation` once, preserving first occurrence
- `prompt_inputs.skill_documents` contains a frozen snapshot for
  `md-validation`

Also verify `uv run meridian chat log <chat_id> --last 20` shows a
`chat.configured` event after first acquisition.

Do **not** accept any behavior where chat invents a chat-only agent/skill
instruction string instead of persisting resolved prompt inputs in
`policy.json`.

Stop the server with `Ctrl-C` before moving on.

## 4. Restart / reacquisition uses the persisted policy snapshot

Use a fresh runtime root. Start chat with the reviewer/skill/approval command
from section 3, create a chat, and send one prompt so the backend is acquired.

Before stopping the server, record:

- `chat_id`
- `"$MERIDIAN_HOME/chats/<chat_id>/policy.json"`
- the `snapshot_id`, `canonical_model_id`, `harness`, `approval`, and frozen
  `prompt_inputs` content from that file

Choose one of these two stop variants before proceeding:

- **Variant A — stop while idle**: wait for the prompt turn to finish (chat
  reaches `idle` state), then stop the server with `Ctrl-C`.
- **Variant B — stop mid-turn**: kill the server with `Ctrl-C` while the turn
  is still in progress (`active` or `draining`).

Restart the server against the same `MERIDIAN_HOME` but with intentionally
different launch inputs, for example:

```bash
uv run meridian chat --headless --port 8765 --harness claude -m haiku --approval confirm
```

Expected after restart:

- `GET /chat/<chat_id>/state` reports `idle`.
- `uv run meridian chat log <chat_id> --last 20` replays prior history.
  - **Variant B only**: the log includes exactly one `runtime.error` with
    `reason` `backend_lost_after_restart`. This event is **not** expected for
    Variant A — the chat was already idle when the server stopped, so recovery
    is clean with no error event.
- `"$MERIDIAN_HOME/chats/<chat_id>/policy.json"` is unchanged from the file you
  recorded before restart.
- The recovered chat does **not** silently adopt the new server flags
  (`haiku`, `claude`, `confirm`) for the existing `chat_id`.

Optional stronger drift check, only in a disposable worktree or throwaway copy
of the repo:

- after the first chat is created, change the source content for the chosen
  agent or skill
- restart the server
- verify the recovered chat still uses the already-persisted `policy.json`
  prompt inputs rather than reloading edited source files

If you cannot safely run that stronger source-drift probe live, rely on the
policy snapshot fallback tests listed above.

## 5. Management subcommands reject launch-policy flags

These negative checks should fail during argument parsing:

```bash
uv run meridian chat ls --model codex
uv run meridian chat show c1 --approval auto
uv run meridian chat log c1 --skills md-validation
uv run meridian chat close c1 --agent reviewer
```

Expected for each:

- command exits non-zero
- error contains `Unknown option:`
- the rejected flag name appears in the error output

Do **not** accept behavior where runtime-management commands silently accept or
ignore launch-policy flags.
