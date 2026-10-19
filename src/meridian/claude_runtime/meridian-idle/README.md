# Meridian idle plugin

A Claude Code mod that tells Meridian when a session goes idle, what work the
turn left running, and when the user comes back. It also displays the active
Meridian spawns running under the primary session, including nested spawns.
Meridian decides what idle action to take and produces the bounded status text;
the mod only senses, runs the timers Meridian places, and displays that text.

## What it does

Every `meridian idle` command below carries `--interactive`; the table omits
that common flag for readability.

| Claude hook | Meridian call | Why |
|---|---|---|
| `session.start` (TUI only) | `meridian idle config --harness claude` | Stays inert unless the role gate and the config say `enabled`. Under `claude -p` (`isInteractive === false`, no surface) it never calls Meridian. |
| `session.start`, after a reload or resume | `meridian idle status --json` | Rebuilds timers from the stored schedule of this session's open stretch, skipping stages already done. |
| `session.start`; a main `turn.complete`; completion of a Bash command matching `\bmeridian\b(?:\s+-\S+)*\s+spawn\b` | `meridian idle child-status` | Core selects the derived-active, non-primary spawns owned by this session's chat and returns a bounded line such as `2 running: p123 coder, p124 reviewer`. The mod puts it in `$.ui.status`; successful empty output clears the line. Unrelated Bash, Read, and Edit calls do not pay for a Meridian startup. |
| `turn.complete` without `agentId` | `meridian idle arm --harness claude --session <id> [--background-tasks=<json>] [--user-text=...] [--assistant-text=...]` | A main-loop turn ended. Core resolves the recorded native transcript store through the inherited `MERIDIAN_CHAT_ID`; the mod never derives it from the session's mutable cwd. The mod sends the last composer prompt and the event's answer as notification excerpts; injected origins and system reminders are excluded. `--background-tasks` forwards a bounded prefix of the `background_tasks` from the `classic.Stop` that precedes the turn's end, each as `{kind, command?}`: subagents and workflows are `agent`, monitors `watcher`, and shells are `shell` when their full command fits the bound or `other` otherwise. Core alone decides what they mean: it holds the push while work that wakes the session runs (that turn arms again), delays it for other tasks, and drops the warning when a `spawn wait` yields before the cache goes cold. A new or re-anchored schedule returns absolute `push_at` / `warn_at` (and `push_held` with the reason, for the log); an absorbed arm keeps matching timers or recovers them from `status`. |
| `classic.Notification` with `notification_type = "permission_prompt"` | `meridian idle needs-permission --harness claude --message <text>` | Claude reports that a main-loop or background-subagent permission prompt is still unanswered. Meridian forwards one stateless `notify.needs_permission` notice; other notification types are ignored. |
| `prompt.submit` or `command.run` with origin `composer` (or `bridge`, the user's phone) | `meridian idle return --harness claude --session <id> --user-prompt` | The user is back. Timers are cancelled first, synchronously. |
| timer: push, warn | `meridian idle fire push\|warn …` | Core sends the notification itself. The mod only reports that the timer fired. |
| timer: while spawn status is nonempty | `meridian idle child-status` every 60 seconds | Catches autonomous spawn completion when no Claude event fires. Sixty seconds bounds stale display while limiting the current ~0.8-second CLI startup cost to periods with active spawns; polling stops as soon as core returns empty. Transient failures preserve the last good line and retry on the slow timer. |

Injected prompts (`task-notification`, `peer`, `plugin`, `sdk`, scheduled
triggers, Stop-hook continuations) are not returns.

The mod never sends a notification, reads Meridian files or `MERIDIAN_*`
variables, writes under `~/.meridian`, or prints into the transcript. Its only
persistent UI is the core-produced child line passed to `$.ui.status`, which is
separate from the user's configured `statusLine`. Its own diagnostics live in
the plugin's `$.store` under the key `log` (last 100 lines).

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
