# Meridian idle plugin

A Claude Code mod that tells Meridian when a session goes idle, what work the
turn left running, when Claude needs a permission decision, and when the user
comes back. Meridian decides what to do about it: a push notification, and a
warning that the prompt cache is about to expire. The mod only senses and runs
the timers Meridian places.

## What it does

Every `meridian idle` command below carries `--interactive`; the table omits
that common flag for readability.

| Claude hook | Meridian call | Why |
|---|---|---|
| `session.start` (TUI only) | `meridian idle config --harness claude` | Stays inert unless the role gate and the config say `enabled`. Under `claude -p` (`isInteractive === false`, no surface) it never calls Meridian. |
| `session.start`, after a reload or resume | `meridian idle status --json` | Rebuilds timers from the stored schedule of this session's open stretch, skipping stages already done. |
| `turn.complete` without `agentId` | `meridian idle arm --harness claude --session <id> [--background-tasks=<json>] [--user-text=...] [--assistant-text=...]` | A main-loop turn ended. Core resolves the recorded native transcript store through the inherited `MERIDIAN_CHAT_ID`; the mod never derives it from the session's mutable cwd. The mod sends the last composer prompt and the event's answer as notification excerpts; injected origins and system reminders are excluded. `--background-tasks` forwards a bounded prefix of the `background_tasks` from the `classic.Stop` that precedes the turn's end, each as `{kind, command?}`: subagents and workflows are `agent`, monitors `watcher`, shells `shell` with a bounded command prefix. Core alone decides what they mean: it holds the push while work that wakes the session runs (that turn arms again), delays it for other tasks, and drops the warning when a `spawn wait` yields before the cache goes cold. A new or re-anchored schedule returns absolute `push_at` / `warn_at` (and `push_held` with the reason, for the log); an absorbed arm keeps matching timers or recovers them from `status`. |
| `classic.PermissionRequest` | `meridian idle human-needed --harness claude --session <id>` | Claude has shown a permission prompt, including one from a background subagent. The mod first awaits downstream and reports only when no hook supplied a decision; it never answers or changes the result. Core persists the need even during a running main turn, releases a held push after `push_seconds`, and prevents later arms from holding it again. The warning schedule is preserved. |
| resolution of the matching `tool.call` after a reported `PermissionRequest` | `meridian idle return --harness claude --session <id> --user-prompt` | The person approved (normal tool result) or denied (`isError: true`) that prompt. The mod correlates the loop, tool name, and input to the in-flight call, so an auto-allowed call never counts. Timers are cancelled first, synchronously; the agent's later wake turn arms fresh. |
| `prompt.submit` or `command.run` with origin `composer` (or `bridge`, the user's phone) | `meridian idle return --harness claude --session <id> --user-prompt` | The user is back. Timers are cancelled first, synchronously. |
| timer: push, warn | `meridian idle fire push\|warn …` | Core sends the notification itself. The mod only reports that the timer fired. |

Injected prompts (`task-notification`, `peer`, `plugin`, `sdk`, scheduled
triggers, Stop-hook continuations) are not returns.

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
