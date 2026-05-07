External research question: Does OpenCode provide a side-channel API/protocol suitable for Meridian to observe and control an interactive session without wrapping the TUI in a PTY?

Context:
- User asks if Meridian could listen to both sides of a websocket and eventually inject commands, and whether that works for OpenCode.
- Need current facts from official OpenCode docs/source if available.

Task:
Use current official/primary sources only. Answer:
1. Does OpenCode expose an app-server, websocket, JSON-RPC, event stream, or similar API for session observability?
2. Does it support attaching a TUI to a remote session while another observer listens?
3. Does it support injecting prompts/commands or steering an existing session through an API?
4. Does it expose runtime permission/tool approval requests over that API?
5. If not, what is the closest supported mechanism?

Cite links/sources. Do not speculate beyond sources; mark inferences clearly.
