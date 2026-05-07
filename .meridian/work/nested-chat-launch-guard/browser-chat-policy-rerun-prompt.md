Rerun the browser/live chat shared-policy test now that nested chat launch and management guards were removed in commits fdad9656 and 09c4dc23.

Context:
- Previous browser test p4988 was BLOCKED by the nested chat launch guard.
- Now `meridian chat --headless` should launch from nested spawns.
- Work item: nested-chat-launch-guard.

Test goals:
1. Start the local chat/headless server from this nested spawn using current repo source:
   `uv run meridian chat --headless --port 0 --harness codex -m codex`
2. Confirm it starts and captures the backend URL.
3. Use browser/HTTP tooling as appropriate to verify the live surface is reachable.
4. Verify evidence that `-m codex` resolves through shared policy to canonical `gpt-5.3-codex` if observable from logs/state/API. If not observable, say so and run the best adjacent live/focused check.
5. Also test agent/skills flags if practical:
   `--agent meridian-subagent --skills playwright-cli --approval auto`
6. Do not fake root execution by mutating MERIDIAN_DEPTH.
7. Cleanly stop any server you start. Do not kill unrelated user servers.
8. Do not modify source files.

Return concise PASS/FAIL with commands, URLs, browser/HTTP evidence, and limitations.
