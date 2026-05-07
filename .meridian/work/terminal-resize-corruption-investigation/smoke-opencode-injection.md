Smoke-test OpenCode ability to support two Meridian control-plane injection cases without relying on PTY rendering.

Repo: /home/jimyao/gitrepos/meridian-cli
Work item: .meridian/work/terminal-resize-corruption-investigation
Context: User is exploring no-PTY Codex/OpenCode architecture where Meridian stays on event/control path. Need runtime facts for injection timing.

Cases to test for OpenCode:
1. Interrupt current turn, like pressing Escape/cancel/abort, then inject a user message.
2. Inject-only: send a user message into an active session without interrupting first.

Constraints:
- Do not edit source files.
- Do not revert/stash/reset/delete anything.
- Avoid expensive/long prompts; use cheap/short prompts where model selection is possible.
- Prefer installed `meridian` for spawn mechanics. Use `uv run meridian` only when deliberately testing local source behavior.
- Keep sessions bounded and cleanly exit/finish where practical.
- If auth/tooling blocks a probe, report blocker and the exact command/output.

Suggested angles:
- Use OpenCode `opencode serve` + HTTP/SSE APIs directly if practical.
- Use existing Meridian OpenCode connection behavior if there is a CLI/API route.
- We need whether API injection works, whether abort+prompt works, and limitations.

Deliverable:
Write `.meridian/work/terminal-resize-corruption-investigation/opencode-injection-smoke.md` with:
- commands/procedure used;
- result for interrupt+inject;
- result for inject-only;
- whether user-message injection appears as a normal session message;
- whether permission/HITL behavior is involved or untested;
- risks/limitations for no-PTY architecture.
