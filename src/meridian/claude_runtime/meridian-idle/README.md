# Meridian idle plugin

A Claude Code mod that tells Meridian when a session goes idle, what work the
turn left running, and when the user comes back. Meridian decides what to do
about it: a push notification, and a warning that the prompt cache is about to
expire. The mod only senses and runs the timers Meridian places.

## What it does

Every `meridian idle` command below carries `--interactive`; the table omits
that common flag for readability.

| Claude hook | Meridian call | Why |
|---|---|---|
| `session.start` (TUI only) | `meridian idle config --harness claude` | Stays inert unless the role gate and the config say `enabled`. Under `claude -p` (`isInteractive === false`, no surface) it never calls Meridian. |
| `session.start`, after a reload or resume | `meridian idle status --json` | Rebuilds timers from the stored schedule of this session's open stretch, skipping stages already done. |
| `turn.complete` without `agentId` | `meridian idle arm --harness claude --session <id> [--background-tasks=<json>] [--user-text=...] [--assistant-text=...]` | A main-loop turn ended. Core resolves the recorded native transcript store through the inherited `MERIDIAN_CHAT_ID`; the mod never derives it from the session's mutable cwd. The mod sends the last composer prompt and the event's answer as notification excerpts; injected origins and system reminders are excluded. `--background-tasks` forwards a bounded prefix of the `background_tasks` from the `classic.Stop` that precedes the turn's end, each as `{kind, command?}`: subagents and workflows are `agent`, monitors `watcher`, and shells are `shell` when their full command fits the bound or `other` otherwise. Core alone decides what they mean: it holds the push while work that wakes the session runs (that turn arms again), delays it for other tasks, and drops the warning when a `spawn wait` yields before the cache goes cold. A new or re-anchored schedule returns absolute `push_at` / `warn_at` (and `push_held` with the reason, for the log); an absorbed arm keeps matching timers or recovers them from `status`. |
| `classic.Notification` with `notification_type = "permission_prompt"` | `meridian idle needs-permission --harness claude --message <text>` | Claude reports that a main-loop or background-subagent permission prompt is still unanswered. Meridian forwards one stateless `notify.needs_permission` notice; other notification types are ignored. |
| `prompt.submit` or `command.run` with origin `composer` (or `bridge`, the user's phone) | `meridian idle return --harness claude --session <id> --user-prompt` | The user is back. Timers are cancelled first, synchronously. |
| timer: push, warn | `meridian idle fire push\|warn …` | Core sends the notification itself. The mod only reports that the timer fired. |
| `fire warn` answers `act`, keepwarm on | `$.prompt.submit` (ping 1) | The cache warning fired and the user opted in: keep the cache warm. See Keepwarm. |
| `turn.start`, `turn.complete`, `tool.check` (main loop) | none | Find the ping's turn by its nonce, read its reply, and deny tools that change anything while it runs. |
| `prompt.edit`, `prompt.change` | none | Remember when the user last touched the prompt box, so a ping waits for a quiet one. |

Injected prompts (`task-notification`, `peer`, `plugin`, `sdk`, scheduled
triggers, Stop-hook continuations) are not returns.

The mod never sends a notification, reads Meridian config files or
`MERIDIAN_*` variables, writes under `~/.meridian`, or prints to the terminal.
Its own diagnostics live in the plugin's `$.store` under the key `log` (last
100 lines).

## `/meridian-idle`

Prints the current stretch, anchor, time left on each timer, the keepwarm state
and tally, and the last log lines. It does not count as a return.

`/meridian-idle keepwarm on` and `/meridian-idle keepwarm off` set keepwarm.

## Keepwarm

Off until `/meridian-idle keepwarm on`; the model cannot turn it on. A restart
or `/clear` drops the setting (`$.state` is reset), a hot reload keeps it.

With it on, the cache warning (core's `warn` stage, `idle.warn_minutes` before
the cache expires) is followed by a prompt that keeps the cache warm: ping 1
when `fire warn` answers `act`, then up to two more. Core fires the warning once
per stretch, so the mod times pings 2 and 3 itself: `lead` (the gap core placed
between the turn and the warning, taken from the arm that placed it) after each
later main-loop arm of the same stretch. A new main-loop arm replaces the timer,
a user return cancels it, and a timer that runs more than 60 s late is skipped
because the cache may be cold. The count is kept per core stretch, in
`$.state`, before each submit, so a reload can cost a ping but never add one.
Pings send no notification.

- **Quiet box.** A ping is submitted only after the prompt box has been empty
  and untouched for 10 s. It gives up when about one lead has passed.
- **Not the user.** The ping is a plugin prompt without `asUser`, so the model
  reads it as sent by the plugin, and the mod does not count it as a return.
- **Its turn.** The text carries a nonce; the main-loop `turn.start` that
  carries it names the ping's `turnId`, and only that turn's `turn.complete`
  with reason `answer` is read. A turn already running when the warning fires
  is not mistaken for it.
- **Tools.** While the ping's turn runs, main-loop tool calls that are not
  `isReadOnly` are denied. Subagent loops and the rest of the session are not
  touched. The denied tool names go in the log record.
- **Stop.** A reply that is exactly `stop-waiting` (alone or in one pair of
  quotes or backticks) ends this stretch's pings; keepwarm stays on.
- **Your-turn push.** The ping turn's arm sends the last answer from a
  non-ping turn as `--assistant-text`, so the next notice does not read "ok".
  That answer is also kept in `$.store` key `answer` (with its session id), so
  it survives a resume or hot reload; another session's value is ignored.

Each submitted ping adds one record (time, session, stretch, ping number,
`stopped`, `continued` or `failed`, denied tools, reply excerpt) to the plugin
`$.store` key `keepwarm`, the last 200. Two sessions appending at once can lose
a record. A `continued` reply that is just an acknowledgement means the model
waited; denied tools or a reply that starts work mean it did not.

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
