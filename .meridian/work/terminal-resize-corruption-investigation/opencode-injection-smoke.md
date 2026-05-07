# OpenCode injection smoke test

Date: 2026-05-07
Environment:
- OpenCode `1.14.35`
- Disposable working dir: `/tmp/opencode-inject.85WQuz`
- Headless server command: `opencode serve --hostname 127.0.0.1 --port 4096 --print-logs`

## Procedure used

1. Start headless OpenCode server:
   ```bash
   opencode serve --hostname 127.0.0.1 --port 4096 --print-logs
   ```
2. Create a fresh session:
   ```bash
   curl -sS -X POST http://127.0.0.1:4096/session \
     -H 'Content-Type: application/json' -d '{}'
   ```
3. Start an async long-running turn using the `bash` tool:
   ```bash
   curl -sS -X POST http://127.0.0.1:4096/session/$SID/prompt_async \
     -H 'Content-Type: application/json' \
     -d '{"parts":[{"type":"text","text":"Use the bash tool to run exactly: sleep 30; echo TOOL_DONE. After the tool returns, reply with exactly TOOL_DONE."}]}'
   ```
4. Inspect state with:
   ```bash
   curl -sS http://127.0.0.1:4096/session/$SID/message | jq .
   ```
5. For inject-only, post a user message during the in-flight turn:
   ```bash
   curl -i -sS -X POST http://127.0.0.1:4096/session/$SID/message \
     -H 'Content-Type: application/json' \
     -d '{"parts":[{"type":"text","text":"After your current work, reply with exactly INJECT_ONLY_ACK."}]}'
   ```
6. For interrupt+inject, wait until the `bash` tool reports `state.status == "running"`, then:
   ```bash
   curl -i -sS -X POST http://127.0.0.1:4096/session/$SID/abort
   curl -i -sS -X POST http://127.0.0.1:4096/session/$SID/message \
     -H 'Content-Type: application/json' \
     -d '{"parts":[{"type":"text","text":"Reply with exactly AFTER_RUNNING_ABORT_ACK."}]}'
   ```
7. Quick observer-path sanity check:
   ```bash
   curl -i -N --max-time 2 http://127.0.0.1:4096/event
   curl -i -N --max-time 2 http://127.0.0.1:4096/global/event
   ```

## Result: inject-only

Outcome: **works, queued behind the active turn**.

Evidence:
- Long-running prompt produced an assistant message with a `bash` tool part that reached `pending` then `running`.
- Posting `POST /session/$SID/message` during that turn succeeded.
- The HTTP call did **not** return immediately; it blocked until OpenCode finished the active turn and then completed the injected turn.
- Final session log showed a normal user message followed by a normal assistant reply:

```json
{
  "role": "user",
  "id": "msg_e022746b5001KZyKxOOBj01prQ",
  "parts": [{"type": "text", "text": "After your current work, reply with exactly INJECT_ONLY_ACK."}]
}
{
  "role": "assistant",
  "parentID": "msg_e022746b5001KZyKxOOBj01prQ",
  "finish": "stop",
  "parts": [{"type": "text", "text": "INJECT_ONLY_ACK"}]
}
```

Interpretation:
- User-message injection appears in session history as a **normal user turn**.
- Inject-only does **not** preempt the active turn.
- `POST /session/$SID/message` behaves like **queue + wait for reply**, not fire-and-forget enqueue.

## Result: interrupt + inject

Outcome: **works**.

Evidence:
- I polled until the active assistant message had a `bash` tool part with `state.status == "running"`.
- `POST /session/$SID/abort` returned `true` in about 14 ms.
- The aborted assistant message then contained:
  - `info.error.name = "MessageAbortedError"`
  - a `bash` tool part with `state.input.command = "sleep 30; echo TOOL_DONE"`
  - `state.output` containing:
    ```text
    (no output)

    <shell_metadata>
    User aborted the command
    </shell_metadata>
    ```
- After abort, injecting a new user message succeeded and OpenCode replied with `AFTER_RUNNING_ABORT_ACK`.

Representative aborted assistant record:

```json
{
  "info": {
    "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}}
  },
  "parts": [
    {"type": "step-start"},
    {
      "type": "tool",
      "tool": "bash",
      "state": {
        "status": "completed",
        "input": {"command": "sleep 30; echo TOOL_DONE"},
        "output": "(no output)\n\n<shell_metadata>\nUser aborted the command\n</shell_metadata>"
      }
    }
  ]
}
```

Interpretation:
- Abort really does stop the active tool run promptly.
- The post-abort injected message appears as a **normal user turn** and gets a normal assistant reply.
- Important nuance: aborted tool execution is surfaced as tool `state.status = "completed"` plus `MessageAbortedError` and abort metadata. A control-plane client cannot rely on tool status alone to detect interruption.

## Whether injection appears as a normal session message

Yes.

In both cases, the injected input was stored as a normal `role: "user"` message in `GET /session/$SID/message`, and the follow-up assistant reply had `parentID` pointing to that injected user message.

## Permission / HITL behavior

Status: **untested in these runs**.

Observed facts:
- `GET /permission` returned `[]` during the probe.
- `GET /question` returned `[]` during the probe.
- The `bash` tool ran and was abortable without a permission round-trip in this temp-dir setup.

So this smoke test confirms message injection and abort behavior, but it does **not** verify Meridian-style runtime permission forwarding or HITL reply plumbing for a no-PTY architecture.

## Risks / limitations for a no-PTY architecture

1. **Message injection works, but `/message` is blocking.**
   A controller that calls `POST /session/$SID/message` during an active turn should expect queueing plus waiting for the injected turn's reply, not an immediate enqueue ack.

2. **Abort detection needs protocol-aware handling.**
   On abort, the tool part can say `status: completed` even though the command was cut short. Reliable detection requires checking assistant-level error fields and tool output metadata, not just part status.

3. **Observer stream only lightly checked.**
   `/event` and `/global/event` both exposed SSE and emitted `server.connected`, so a side-channel observer path exists. I did **not** verify ordered per-turn event semantics, reconnect behavior, or whether the SSE stream alone is sufficient for Meridian hooks.

4. **Permission forwarding remains open risk.**
   Because these runs never hit permission prompts, they do not answer whether a no-PTY Meridian control path can forward permission requests/replies correctly without regressing into default-deny behavior.

5. **No multi-client TUI concurrency check here.**
   I did not run a live attached TUI client at the same time as HTTP injection, so this report confirms server-side control behavior but not simultaneous TUI repaint/update behavior.

## Bottom line

OpenCode's headless HTTP control path is sufficient for both requested Meridian control-plane cases:
- **inject-only**: supported, queued behind the active turn
- **interrupt then inject**: supported, with prompt abort and successful follow-up message injection

The main caution for a no-PTY Meridian design is not basic capability; it is **state interpretation**:
- use protocol-aware abort detection,
- do not assume `/message` is non-blocking,
- and separately verify permission/HITL forwarding plus ordered observer-stream semantics.
