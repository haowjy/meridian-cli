# No-PTY Sidechannel Control Plane

## Problem statement
Meridian currently mediates interactive harness TUIs through PTY wrapping in cases where the harness already has a side-channel control/observation API. This can make terminal rendering diverge from the native harness experience, especially for Codex remote TUI sessions, where users have observed repaint/layout corruption during resize, idle, and focus-return scenarios.

The problem is not "remove PTY everywhere." Claude still needs PTY mediation because it lacks an equivalent side-channel boundary. The problem is to make terminal mediation harness-specific: keep Meridian on the session event/control path while letting harnesses with proper side-channel APIs own terminal rendering directly.

## User intent
The user wants Meridian to:
- stop wrapping Codex and likely OpenCode interactive TUIs in PTY when side-channel control exists;
- keep PTY for Claude;
- remain able to observe events/commands and run hooks;
- support future event-loop behavior where external events or hooks inject new user messages into an active session;
- support two injection timing modes:
  1. interrupt active turn, like Escape, then inject a user message;
  2. inject-only: send a user message without interrupting first;
- preserve permission/HITL behavior and avoid repeating prior intercept behavior that appeared to reject all permissions/default-deny.

## Desired architecture direction
Separate rendering from control:

```text
TUI owns real terminal directly
Meridian stays attached to event/control plane
```

Preferred model per harness:

```text
Codex:    app-server/WS side-channel for observe/control; direct terminal for TUI if feasible
OpenCode: HTTP/SSE side-channel for observe/control; direct terminal for TUI if feasible
Claude:   PTY wrapper remains required
```

Avoid transparent websocket MITM/proxy unless no first-class observer/control API can satisfy hooks, injection, and permissions.

## Hook/control semantics
Meridian hooks may behave like: "we saw this command/event; Meridian should also do something." Therefore Meridian cannot fully detach from the control/observation path for harnesses where hooks are required.

Design must distinguish:
- passive observation/logging;
- advisory hooks with non-blocking side effects;
- policy hooks that can allow/deny/modify;
- mutation/injection hooks that send messages or commands back into the session.

## Injection semantics to support
Two explicit cases:

1. **Interrupt + inject**
   - Meridian interrupts/cancels the active turn, like pressing Escape.
   - Then Meridian injects a user message as a normal session message.

2. **Inject-only**
   - Meridian sends a user message without interrupting first.
   - Harness-specific behavior may be same-turn steering, queued next-turn, or blocking request; design must make this explicit.

## Smoke evidence already gathered
Prior investigation artifacts were initially written under `.meridian/work/terminal-resize-corruption-investigation/`. Key findings:

### Codex
- Inject-only works through current `meridian spawn inject` and appears in Codex history as a normal `userMessage`.
- Raw Codex `app-server` exposes `turn/start`, `turn/steer`, and `turn/interrupt`.
- Interrupt + inject works at raw Codex app-server level: `turn/interrupt` succeeds, next `turn/start` is accepted.
- Important risk: in the smoke probe, Codex interrupt was logical, not a hard subprocess kill. Old command output kept streaming after interrupted turn status and interleaved with the next turn.

### OpenCode
- OpenCode has `opencode serve`, HTTP/SSE event streams, session message APIs, abort API, and permission reply endpoints upstream.
- Inject-only works via `POST /session/:id/message`; it queues behind active work and the HTTP call blocks until the injected turn completes.
- Interrupt + inject works via `POST /session/:id/abort` followed by `POST /session/:id/message`; abort stopped the active bash tool promptly in the smoke probe.
- OpenCode abort state requires protocol-aware interpretation: tool part may show completed while assistant message has `MessageAbortedError` and abort metadata.
- Runtime permission/HITL forwarding was not yet verified in the smoke probes.

## Acceptance criteria for design
A design is acceptable if it defines:
- per-harness terminal mode selection: inherited terminal vs PTY wrapper;
- why Claude remains PTY-wrapped;
- Codex no-PTY feasibility path and fallback if app-server cannot support required observer/control semantics;
- OpenCode no-PTY feasibility path using HTTP/SSE APIs;
- how Meridian observes ordered events for hooks without terminal-byte parsing;
- how Meridian injects user messages in both timing modes;
- how runtime permissions/HITL requests are routed and answered without default-deny regressions;
- how stale/interleaved output from interrupted Codex turns is fenced by turn/session IDs;
- how replay/resume avoids refiring one-time hooks accidentally;
- cross-platform implications, especially Windows console inheritance and POSIX terminal inheritance;
- migration plan that minimizes disruption to Claude and existing spawn behavior.

## Constraints
- Do not remove PTY globally.
- Do not degrade Claude behavior.
- Do not rely on git for core state behavior.
- No backwards compatibility requirement if schema changes improve correctness.
- Windows is first-class for terminal/process design.
- Prefer first-class side-channel APIs over protocol MITM/proxying.
- Do not implement until design is approved.
