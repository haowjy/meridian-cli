Investigate the user's report of Codex CLI terminal visual corruption when run through Meridian.

Context:
- Repo: /home/jimyao/gitrepos/meridian-cli
- Screenshot path: /home/jimyao/gitrepos/.clipboard/messed up codex cli.png
- User says it is a messed-up image of the Codex CLI, happens most often during terminal resizing, and they do not really face this problem with normal Codex.
- Requirements are in requirements.md in the active work item.

Task:
1. Inspect the screenshot and summarize the visible failure mode.
2. Investigate, read-only, how Meridian launches/mediates Codex TUI sessions and whether resize handling differs from direct Codex.
3. Identify likely root cause candidates and rank them by evidence.
4. Propose a minimal reproduction/probe plan. If safe and practical, run non-destructive probes that do not require modifying source. Avoid long-running interactive sessions unless clearly bounded.
5. Report recommended next step. Do not implement a fix.

Safety:
- Do not edit files.
- Do not revert/stash/reset/delete anything.
- There are many uncommitted changes from other actors; treat them as owned by others.
- If you need to run tests or probes that may disturb active sessions, stop and report instead.

Deliverable:
Write a concise investigation report to `.meridian/work/terminal-resize-corruption-investigation/investigation-report.md` and include: observations, evidence, likely causes, reproduction/probe plan, and next-step recommendation.
