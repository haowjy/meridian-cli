Research question: For Meridian's current OpenCode harness support, can we avoid PTY wrapping like we are considering for Codex, while retaining observability/control/permissions?

Context:
- User suspects Codex/OpenCode should not need PTY, but Claude does.
- User now asks whether, if Meridian is only listening, Codex will also be listening to the same websocket and whether Meridian needs to observe both sides of the websocket. They also wonder whether Meridian could eventually inject commands, and whether that works for OpenCode.
- Work item requirements are in .meridian/work/terminal-resize-corruption-investigation/requirements.md

Task:
Read the repo only. Do not edit files.
Answer:
1. How Meridian currently launches/observes OpenCode interactive sessions.
2. Whether OpenCode currently has side-channel observability/control in Meridian comparable to Codex app-server websocket.
3. Whether Meridian currently supports command injection/steering for OpenCode, and through what mechanism.
4. Whether runtime permission/HITL requests for OpenCode are mediated through PTY, a side-channel, or not at all.
5. What a no-PTY OpenCode path would require or lose.

Deliver concise findings with file references. Do not implement.
