# Codex injection smoke - 2026-05-07

## Scope
Probe two Codex control-plane cases relevant to a no-PTY Meridian architecture:

1. interrupt current turn, then inject a new user message;
2. inject-only into an active session without interrupting first.

Constraints honored:
- no source edits;
- no revert/stash/reset/delete;
- used installed `meridian` for Meridian-managed probing;
- used cheap Codex model (`gpt-5.4-mini` via `gptmini`) where applicable.

## Environment blocker on Meridian chat surface
Tried the most direct Meridian API/backend route first:

```bash
meridian chat --headless --harness codex -m gptmini --port 0
```

Observed:

```text
error: meridian chat requires a root Meridian process. Chat commands cannot run inside a nested spawn or delegated execution.
```

Because this investigation is running inside a delegated Meridian environment, I could not use the chat backend's explicit `cancel` + `msg` HTTP API from here. I therefore split the probing into:
- Meridian-managed `spawn inject` for inject-only; and
- direct Codex `app-server` JSON-RPC over stdio for interrupt+inject fallback.

---

## Probe 1 - Meridian-managed inject-only

### Procedure
Start a background Codex spawn with a long visible response:

```bash
meridian spawn --bg --work terminal-resize-corruption-investigation \
  --desc 'codex inject-only smoke' --harness codex -m gptmini \
  -p 'Output the string MERIDIAN exactly 5000 times, one per line, and nothing else.'
```

Observed:

```json
{"spawn_id":"p4972","status":"running"}
```

Inject while the spawn is still running:

```bash
meridian spawn inject p4972 \
  --message 'Stop the numbered list immediately. Reply with exactly INJECTED and nothing else.'
```

Observed:

```text
Message delivered to spawn p4972
```

Wait for completion:

```bash
meridian spawn wait p4972
```

Observed terminal status:

```json
{"spawn_id":"p4972","status":"succeeded","exit_code":0}
```

### Evidence
Readable session log:

```bash
meridian session log p4972 | tail -n 120
```

Observed key excerpt:

```text
--- 8 [user] ---
Output the string MERIDIAN exactly 5000 times, one per line, and nothing else.

--- 9 [assistant] ---
MERIDIAN
MERIDIAN
...snip...

--- 10 [user] ---
Stop the numbered list immediately. Reply with exactly INJECTED and nothing else.

--- 11 [assistant] ---
INJECTED
```

Raw Meridian history around the injection:

```bash
cat /home/jimyao/.meridian/projects/71e6b90f-b8c2-4dd6-b608-4dd8f7bf37d5/spawns/p4972/inbound.jsonl
```

Observed:

```json
{"action":"user_message","data":{"text":"Stop the numbered list immediately. Reply with exactly INJECTED and nothing else."},"source":"control_socket","ts":1778152651.558361}
```

Raw Codex history for the same spawn showed the injected text became a normal `userMessage` item inside the Codex thread:

```json
{"event_type":"item/started","payload":{"item":{"type":"userMessage","content":[{"text":"Stop the numbered list immediately. Reply with exactly INJECTED and nothing else.","text_elements":[],"type":"text"}]},"turnId":"019e0228-2dfd-7043-aabd-e4d842be0549"}}
```

and then a normal assistant message:

```json
{"event_type":"item/completed","payload":{"item":{"type":"agentMessage","text":"INJECTED"},"turnId":"019e0228-2dfd-7043-aabd-e4d842be0549"}}
```

### Result
- **Inject-only: works today through Meridian-managed Codex spawn control.**
- The injected message **does appear as a normal session user message**, not just a Meridian-side note.
- In this run the injected message was attached to the **same Codex turn id** (`019e0228-2dfd-7043-aabd-e4d842be0549`), consistent with Codex same-turn steering behavior.

### Limits of this probe
- No permission/HITL path exercised.
- This used spawn control, not primary/chat control.
- The long output was text-only, so no tool cancellation behavior was tested here.

---

## Probe 2 - raw Codex app-server interrupt + inject fallback

Because `meridian chat` was blocked in this nested environment, I tested the underlying no-PTY Codex control plane directly over stdio.

### Protocol discovery used
Confirmed locally that current Codex exposes the needed methods:

```bash
codex app-server generate-ts --out /tmp/tmp.Ie2J0LM2Hg
```

Observed relevant generated type union:

```text
... "turn/start" ... "turn/steer" ... "turn/interrupt" ...
```

### Procedure
Start app-server:

```bash
codex app-server
```

Initialize and create thread over JSON-RPC.

Then start a long-running turn that launches a shell loop:

```json
{"jsonrpc":"2.0","id":3,"method":"turn/start","params":{
  "threadId":"019e022a-850e-7a42-bdcb-5a88edd3f0ae",
  "approvalPolicy":"never",
  "input":[{
    "type":"text",
    "text":"Run exactly this shell command and do not answer before it finishes: bash -lc 'for i in $(seq 1 100); do echo MERIDIAN; sleep 0.2; done'",
    "text_elements":[]
  }]
}}
```

Observed active command execution:

```json
{"method":"item/started","params":{"item":{"type":"commandExecution","processId":"68773","status":"inProgress"},"turnId":"019e022a-98e2-7f92-a4d8-bdfe8c95446a"}}
```

Interrupt that active turn:

```json
{"jsonrpc":"2.0","id":4,"method":"turn/interrupt","params":{
  "threadId":"019e022a-850e-7a42-bdcb-5a88edd3f0ae",
  "turnId":"019e022a-98e2-7f92-a4d8-bdfe8c95446a"
}}
```

Observed success and interrupted turn state:

```json
{"id":4,"result":{}}
{"method":"turn/completed","params":{"turn":{"id":"019e022a-98e2-7f92-a4d8-bdfe8c95446a","status":"interrupted"}}}
```

Immediately send a new user turn after the interrupt:

```json
{"jsonrpc":"2.0","id":5,"method":"turn/start","params":{
  "threadId":"019e022a-850e-7a42-bdcb-5a88edd3f0ae",
  "input":[{"type":"text","text":"Reply with exactly POST_INTERRUPT_ACCEPTED and nothing else.","text_elements":[]}]
}}
```

Observed:

```json
{"id":5,"result":{"turn":{"id":"019e022a-e7eb-7fc0-af89-1d22c8689824","status":"inProgress"}}}
{"method":"item/completed","params":{"item":{"type":"agentMessage","text":"POST_INTERRUPT_ACCEPTED"},"turnId":"019e022a-e7eb-7fc0-af89-1d22c8689824"}}
{"method":"turn/completed","params":{"turn":{"id":"019e022a-e7eb-7fc0-af89-1d22c8689824","status":"completed"}}}
```

### Important observed limitation
After `turn/interrupt` succeeded and after the next turn had already started, the old command from the interrupted turn **kept emitting output**:

```json
{"method":"item/commandExecution/outputDelta","params":{"turnId":"019e022a-98e2-7f92-a4d8-bdfe8c95446a","delta":"MERIDIAN\n"}}
```

Those stale output events continued **interleaved with** the new turn's events, and the old command eventually finished normally:

```json
{"method":"item/completed","params":{"item":{"type":"commandExecution","status":"completed","durationMs":20186,"exitCode":0},"turnId":"019e022a-98e2-7f92-a4d8-bdfe8c95446a"}}
```

So in this probe, `turn/interrupt` was a **logical turn interrupt** but **not a hard subprocess kill**.

### Result
- **Interrupt + inject: supported by the underlying Codex app-server control plane.**
- A new user turn can be accepted immediately after interrupt.
- **But interrupted-turn command output can continue after the interrupt and even after the next turn starts.**

---

## Answers to requested questions

### Result for interrupt+inject
- **Yes, underlying Codex app-server supports it.**
- Evidence: `turn/interrupt` returned `{}`, old turn reported `status:"interrupted"`, and subsequent `turn/start` returned `POST_INTERRUPT_ACCEPTED`.
- Meridian's explicit `cancel` + `msg` chat surface was **not directly runnable here** because nested delegated execution blocks `meridian chat`.

### Result for inject-only
- **Yes, Meridian supports it today for Codex spawns.**
- Evidence: `meridian spawn inject p4972 ...` delivered successfully and the session continued with normal user/assistant messages ending in `INJECTED`.

### Does injected user text appear as a normal session message?
- **Yes.**
- Meridian session log showed a normal `[user]` message.
- Raw Codex history showed `item.type == "userMessage"` for the injected text.

### Permission/HITL behavior involved or untested?
- **Mostly untested here.**
- Meridian inject-only probe used text-only output; no approval or user-input request was triggered.
- Raw app-server interrupt probe explicitly set `approvalPolicy:"never"` to avoid permission prompts while testing control timing.
- Therefore this smoke test does **not** verify runtime approval forwarding semantics for a no-PTY design.

---

## Risks / limitations for a no-PTY architecture
1. **Good news:** Codex already exposes first-class no-PTY control primitives (`turn/start`, `turn/steer`, `turn/interrupt`). A Meridian observer/controller does not need websocket MITM just to inject or interrupt.
2. **Important risk:** `turn/interrupt` does **not necessarily stop prior tool/process output immediately**. In the 100-line shell-loop probe, old-turn `commandExecution/outputDelta` events kept arriving after interrupt and after the next turn started.
3. That means a no-PTY Meridian design must handle **ordered, interleaved, stale-turn output** correctly or it risks:
   - UI corruption / replay confusion,
   - mixed output from old and new turns,
   - incorrect remote observer rendering,
   - accidental attribution of old tool output to the new turn.
4. This late-output behavior is a plausible contributor to the broader Codex remote-attach/render-corruption investigation: even without PTY repaint issues, the control plane itself can yield post-interrupt event overlap.

## Practical next step
For implementation/design work, treat Codex no-PTY as **feasible but event-order-sensitive**:
- use side-channel control/observation instead of PTY where possible;
- key rendering and replay strictly by `threadId` + `turnId` + item ids;
- explicitly define how Meridian suppresses or visually isolates stale output from interrupted turns;
- do a follow-up smoke test on the real primary/chat surface once run from a root Meridian process, specifically checking whether Meridian's own observer/replay layer preserves turn separation when old command output continues after interrupt.
