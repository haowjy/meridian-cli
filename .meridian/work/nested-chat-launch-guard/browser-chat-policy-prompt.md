Browser-test the previous chat shared-policy work from `codex-live-smoke-investigation` as far as possible.

Context:
- Recent work made `meridian chat` use shared policy resolution for model/harness/agent/skills/approval.
- The important live flow is `meridian chat --headless --harness codex -m codex` resolving `codex -> gpt-5.3-codex`, launching a chat backend, and exposing a usable browser surface.
- A known blocker may exist: nested Meridian executions currently fail chat launch with "meridian chat requires a root Meridian process". If you hit this, report it clearly as the blocker rather than faking root by changing MERIDIAN_DEPTH.

Test goals:
1. Try to start the local chat/headless server using the current repo source (`uv run meridian chat --headless ...`) with codex alias flags.
2. If it starts, use a browser to open the chat UI/API surface and verify the visible/live behavior relevant to chat launch.
3. Verify whether agent/skills policy can be represented through launch if practical.
4. Do not modify source files. Do not fake root execution by mutating MERIDIAN_DEPTH.
5. Return concise PASS/FAIL/BLOCKED with exact commands, URL(s), screenshots if captured, and whether the blocker is the nested chat launch guard.
