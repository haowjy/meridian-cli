# Meridian first-turn plugin

A Claude Code mod that submits an interactive Meridian launch's starting prompt
(`USER_TASK_PROMPT` + `TASK_CONTEXT`) as the first user turn.

Claude takes a starting prompt as a positional argument, which puts the whole
prompt on the process command line: `ps` shows it, and a `pkill -f <word>`
anywhere on the machine kills the session if the word is in the prompt. This
mod delivers it from a file instead.

## How it works

- Meridian sets `_MERIDIAN_CLAUDE_FIRST_TURN_PATH` to the launch's
  `starting-prompt.md` and omits the positional argument. It sets the variable
  only for a fresh interactive launch that has a starting prompt and loaded this
  plugin; a resume or fork keeps the positional argument.
- On `session.start` (TUI only) the mod starts a background task that reads the
  file and calls `$.prompt.submit({ text, asUser: true })`. The turn arrives
  about a second after launch, also under `bypassPermissions`. The transcript
  row is `type: user` with `origin: {kind: "plugin", asUser: true}`.
- `session.start` fires again on resume and reload, so the mod submits only
  when `$.session.turns()` is 0.

The plugin is separate from `meridian-idle` and `meridian-spawn-watch` because
it has one job and a different load rule: launch delivery, not a sensor or a
policy. `meridian-idle` treats `plugin`-origin prompts as injected, not as a
user return, so the first turn does not cancel an idle timer.

```bash
claude plugin validate src/meridian/claude_runtime/meridian-first-turn
```
