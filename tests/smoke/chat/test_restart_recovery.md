# Chat backend restart recovery smoke test

Run this guide from a top-level terminal. For launch-policy restart drift
coverage, pair it with `tests/smoke/chat/test_shared_policy_surface.md`.

1. Use a disposable runtime root:
   `export MERIDIAN_HOME="$(mktemp -d)"`.
2. Start chat and create a chat that has an on-disk policy snapshot:
   - minimal path: `uv run meridian chat --headless --port 8765`
   - shared-policy path: `uv run meridian chat --headless --port 8765 -a reviewer --skills md-validation --approval auto`
3. Create a chat and verify `"$MERIDIAN_HOME/chats/<chat_id>/policy.json"` exists
   before first acquisition.
4. Send a prompt so the backend is acquired.
5. Stop the server without closing the chat.
   - **Variant A — stop while idle** (turn is complete before you stop): the chat was
     in `idle` state at process death.
   - **Variant B — stop mid-turn** (interrupt a running turn with `Ctrl-C` before it
     completes): the chat was in `active` or `draining` state at process death.
6. Restart the server against the same Meridian runtime root.
7. Verify `GET /chat/<chat_id>/state` returns `idle`.
8. Reconnect `WS /ws/chat/<chat_id>` and verify persisted history replays.
   - **Variant B only**: a `runtime.error` event with `reason: backend_lost_after_restart`
     must appear because the backend was lost while a turn was in progress. This event
     is not expected for Variant A (idle at stop) — the recovered chat transitions
     cleanly to `idle` without a runtime error.
9. Verify the existing `policy.json` file is still present for the recovered
   chat. If you restarted the server with different launch flags, verify the
   recovered chat still uses the old snapshot rather than adopting the new
   server defaults.
