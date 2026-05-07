Implement the requested change: remove the nested `meridian chat` launch guard so delegated agents/spawns can run live chat smoke tests.

User intent:
- The current guard makes `uv run meridian chat --headless ...` fail inside Meridian spawns with: "meridian chat requires a root Meridian process. Chat commands cannot run inside a nested spawn or delegated execution."
- This prevented previous browser/smoke testers from running real chat E2E.
- User explicitly wants the nested chat launch guard changed/removed.

Scope:
- Find the guard that blocks chat startup based on `MERIDIAN_DEPTH` / nested execution and remove or narrow it so chat launch commands can run from spawns.
- Do NOT bypass shared policy resolution. Chat must still use the shared model/harness/agent/skills/approval resolution path.
- Keep management command behavior intact unless tests show the guard is incorrectly applied there too.
- Add/update tests proving nested chat launch is no longer rejected solely due to depth.
- Prefer a focused minimal change. If you discover a real safety issue that makes simple removal unsafe, implement the narrowest targeted safety instead and explain it.

Verification required:
- Run focused tests for chat CLI/policy behavior.
- Run a live local-source CLI smoke from a nested-depth environment that previously failed, e.g. with `MERIDIAN_DEPTH=2`, proving it no longer errors with the root-process guard. Use safe/headless/dry or short-lived mode if available; avoid leaving long-running servers behind.
- Run ruff/pyright on touched files if practical.

Constraints:
- Shared repo: never revert/stash/reset/delete unknown files.
- Stage/commit only files you changed if tests pass. Use a descriptive commit message.
- Report changed files, tests run, commit hash, and any remaining limitations.
