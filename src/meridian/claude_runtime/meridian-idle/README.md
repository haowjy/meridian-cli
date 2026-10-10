# Meridian idle plugin

A Claude Code mod that tells Meridian when a session goes idle and when the
user comes back. Meridian decides what to do about it: a push notification, a
warning that the prompt cache is about to expire, and an optional compaction
before it does. The mod does the sensing and the one action Claude only allows
from inside itself.

## What it does

Every `meridian idle` command below carries `--interactive`; the table omits
that common flag for readability.

| Claude hook | Meridian call | Why |
|---|---|---|
| `session.start` (TUI only) | `meridian idle config --harness claude` | Stays inert unless the role gate and the config say `enabled`. Under `claude -p` (`isInteractive === false`, no surface) it never calls Meridian. |
| `session.start`, after a reload or resume | `meridian idle status --json` | Rebuilds timers from the stored schedule of this session's open stretch, skipping stages already done. |
| `turn.complete` without `agentId` | `meridian idle arm --harness claude --session <id> --cwd <dir> [--user-text ...] [--assistant-text ...]` | A main-loop turn ended. The mod sends the last composer prompt and the event's answer as notification excerpts; injected origins and system reminders are excluded. Skipped while a subagent still runs; the hand-back turn arms later. A new or re-anchored schedule returns absolute `push_at` / `warn_at` / `compact_at`; an absorbed arm keeps matching timers or recovers them from `status`. |
| `prompt.submit` or `command.run` with origin `composer` (or `bridge`, the user's phone) | `meridian idle return --harness claude --session <id> --user-prompt` | The user is back. Timers are cancelled first, synchronously. |
| timer: push, warn | `meridian idle fire push\|warn …` | Core sends the notification itself. The mod only reports that the timer fired. |
| timer: compact | `meridian idle fire compact … <facts>`, then `$.session.compact()` on `act`, then `meridian idle done compact … --result ok\|vetoed\|failed` | Facts: `--draft yes\|no` from `$.prompt.read()`, `--agents-running N` from `$.agent.list()`, `--busy` while a main-loop turn is running (or the agent list is unreadable), `--context-tokens N` from `$.session.usage()`, `--harness-autocompact-off` from the `autoCompact` config row. |

Injected prompts (`task-notification`, `peer`, `plugin`, `sdk`, scheduled
triggers, Stop-hook continuations) are not returns. Compaction emits no turn
events, so it cannot re-arm the timeline.

Compaction only runs when `fire compact` answers `act`, and only from a timer
callback (`$.session.compact()` is rejected inside a `command.run` hook). Right
after `act` the mod looks at the prompt box once more; a draft typed in the
meantime turns the compaction into `done … --result vetoed --reason draft`.

The mod never sends a notification, reads Meridian config files or
`MERIDIAN_*` variables, writes under `~/.meridian`, or prints to the terminal.
Its own diagnostics live in the plugin's `$.store` under the key `log` (last
100 lines).

## `/meridian-idle`

Prints the current stretch, anchor, time left on each timer and the last log
lines. It does not count as a return.

## Where it runs

`meridian claude` adds `--plugin-dir` for interactive launches by itself, and
sets `MERIDIAN_SESSION_ROLE`. To use the mod in a Claude session started
outside Meridian, with `meridian` on `PATH`:

```bash
CLAUDE_CODE_PLUGIN_DIRS=$(meridian idle mod-path) claude
```

Outside Meridian the role is unset and the mod asserts `--interactive`
itself, so only a TUI session ever arms.

## Developing it

```bash
claude plugin validate src/meridian/claude_runtime/meridian-idle
claude plugin test     src/meridian/claude_runtime/meridian-idle
```

The module is plain TypeScript the host runs directly: no build step, no
dependencies. `hooks/register.test.ts` covers each hook path against a fake
`meridian idle` and a fake host.

Claude Code lays build-specific type files into any plugin directory it loads
with `--plugin-dir` or `CLAUDE_CODE_PLUGIN_DIRS`: `.claude-plugin/types/**`
(about 800 KB, different per Claude version, with its own `.gitignore`) and a
56-byte `tsconfig.json` that extends it. The repo ignores `.claude-plugin/types/`
and the wheel excludes it; `tsconfig.json` is committed because it is tiny and
never changes. Claude also loads the mod fine from a read-only directory (an
installed wheel it cannot write to), so the launcher does not copy the mod.
