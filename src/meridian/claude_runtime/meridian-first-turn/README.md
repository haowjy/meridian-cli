# Meridian first-turn plugin

A Claude Code mod that submits an interactive Meridian launch's starting prompt
(`USER_TASK_PROMPT` + `TASK_CONTEXT`) as the first user turn.

Claude takes a starting prompt as a positional argument, which puts the whole
prompt on the process command line: `ps` shows it, and a `pkill -f <word>`
anywhere on the machine kills the session if the word is in the prompt. This
mod delivers it from a file instead.

## How it works

- Meridian sets `_MERIDIAN_CLAUDE_FIRST_TURN_PATH` to the launch's
  `starting-prompt.md` and omits the positional argument, for a fresh, resumed or
  forked interactive launch with a starting prompt. It decides once, in
  `bind_claude_spec`, and keeps the positional argument when the mod will not run:
  mod not found, `--bare` or `--safe-mode` in the passthrough args,
  `CLAUDE_CODE_SIMPLE` or `CLAUDE_CODE_SAFE_MODE` set, or Claude older than 2.1.314.
  The variable is blocked from inheritance, so a launch never gets its parent's.
- On `session.start` (TUI only) the mod starts a background task that reads the
  file and calls `$.prompt.submit({ text, asUser: true })`. The turn arrives
  about a second after launch, also under `bypassPermissions`. The transcript
  row is `type: user` with `origin: {kind: "plugin", asUser: true}`.
- After a successful submit it writes `first-turn-delivered` next to the prompt
  file. `session.start` fires again on reload and worker respawn, also after
  `/clear`, so the mod submits only while that receipt is absent.
- If the submit throws or is dropped, the mod fills the prompt into the composer
  and shows a toast naming the reason; any other failure shows a toast.

The plugin is separate from `meridian-idle` and `meridian-spawn-watch` because
it has one job and a different load rule: launch delivery, not a sensor or a
policy. `meridian-idle` treats `plugin`-origin prompts as injected, not as a
user return, so the first turn does not cancel an idle timer.

```bash
claude plugin validate src/meridian/claude_runtime/meridian-first-turn
```
