# Terminal Resize Corruption Investigation

## User report
User provided screenshot: `/home/jimyao/gitrepos/.clipboard/messed up codex cli.png`.
They report a visually corrupted/messed-up terminal image of the Codex CLI when running through Meridian. It happens most often during terminal resizing. They do not usually see the same issue when running normal Codex directly.

## Problem statement
Meridian-mediated Codex sessions appear to render or repaint incorrectly during terminal resize events, causing visual corruption that does not commonly occur in direct Codex CLI usage.

## Desired outcome
Identify how and why Meridian contributes to the resize/render corruption, including:
- what the screenshot shows;
- which Meridian layer is implicated, if any: spawn wrapper, PTY/session capture, TUI launch, terminal size propagation, output replay, web/chat bridge, or another path;
- how direct Codex differs from Meridian-mediated Codex for resize behavior;
- a minimal reproduction or concrete probe plan;
- whether this is likely a Meridian bug, harness limitation, terminal emulator behavior, or upstream Codex issue;
- recommended next step before implementation.

## Constraints
- Do not modify source code during investigation.
- Do not revert or delete any existing worktree changes; other agents are active.
- Use `meridian`, not `uv run meridian`, for delegation/spawn mechanics. Use `uv run meridian` only if deliberately smoke-testing local source behavior.
- Prefer smoke/runtime evidence over speculative code reading.
- Windows remains first-class if proposed fix touches terminal/process/path behavior.

## Additional evidence
User provided second screenshot path, likely `/home/jimyao/gitrepos/.clipboard/messed up codex cli2.png` (typed as `~/,gitrepos/.clipboard/messed up codex cli2.png`). Treat as another example of the same resize-related Codex CLI corruption.

## Additional behavior notes
User clarified corruption can start without resizing at all: sometimes it appears after the terminal/session is left idle. Recent examples were only a few minutes old, not long-running sessions.
This suggests investigation should not assume resize is the only trigger; consider idle repaint, async output, TUI frame replay, PTY buffering, or resize/refresh events emitted by terminal/window manager even without explicit user resizing.

## Direction refinement
User believes Codex and OpenCode should not need Meridian's PTY wrapper when side-channel observability/control is available, but Claude still needs PTY mediation. Any design should preserve PTY support for Claude while removing/avoiding PTY for Codex/OpenCode where feasible.

## Candidate requirement
Meridian should prefer side-channel observability for harnesses that expose it and let their interactive TUI inherit the real terminal directly. PTY wrapping remains a harness-specific fallback/requirement, currently expected for Claude.

## Permission/replay constraint
User reminder: for replay/remote-observer paths, permissions must be passed through to the remote harness correctly. Prior intercept behavior appeared to reject all permission requests/actions, so a no-PTY remote design must preserve runtime HITL/permission forwarding semantics and must not accidentally default-deny remote tool permissions.

## Side-channel observation/control refinement
User questioned whether Meridian can simply listen to the same remote channel as the TUI, whether it would need to listen to both sides of the websocket, and whether future command injection is possible. Research found OpenCode has HTTP/SSE side-channel APIs for observation, message/command injection, TUI prompt APIs, and permission reply endpoints, but Meridian currently does not implement OpenCode runtime HITL and marks OpenCode supports_steer=False/supports_runtime_hitl=False. Design should prefer first-class side-channel observer/control clients over transparent websocket MITM; MITM/proxying should be a fallback because it risks recreating prior permission default-deny/intercept failures.

## Hook/control-path implication
User clarified that future Meridian hooks are expected to work like: "we saw this command/event, we should do something as well." That means Meridian cannot simply detach from the control/observation path for harnesses where hooks are required. However, the design should still distinguish passive passthrough/tee from active protocol-aware hook handling. If hooks need to observe commands/events and trigger side effects, Meridian must receive a reliable ordered event stream and define whether hooks are advisory side-effects, policy gates, or protocol mutations.

## Injection timing smoke-test request
User wants runtime smoke tests for Codex and OpenCode session-control ability for two cases:
1. Interrupt current turn, like pressing Escape, then inject a user message. Meridian already has an inject concept; verify harness behavior.
2. Inject-only: send a user message without interrupting first.
Injection means sending a user message into the active session, potentially from Meridian/external control while the TUI/control session exists.
