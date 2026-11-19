# Meridian idle plugin

A Claude Code mod that tells Meridian when a session goes idle, what work the
turn left running, and when the user comes back. Meridian decides what to do
about it: a push notification, and a warning that the prompt cache is about to
expire. For those the mod only senses and runs the timers Meridian places.
It submits two kinds of prompt on its own. The state reminder asks the model to
record its state in the session's work item when Meridian's cache warning
fires, then to choose whether to compact now; Meridian decides whether to send it. Keepwarm is off unless
`[idle] keepwarm` (or its `[harness.claude.idle]` override) or `/keepwarm on`
turns it on: it submits up to `keepwarm_pings` (default 3) short prompts so the
cache stays warm while the user is away, the first when the cache warning fires
and the rest on its own timer.

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
| `fire warn` answers `act` with `remind: true` | `$.prompt.submit` (state reminder) | The cache warning fired and core wants the work item's state brought up to date. See State reminder. |
| `fire warn` answers `act` without `remind`, keepwarm on | `$.prompt.submit` (ping 1) | The cache warning fired and the user opted in: keep the cache warm. See Keepwarm. |
| `turn.start`, `turn.complete`, `tool.check` (main loop) | none | Find the ping's or reminder's turn by its nonce, read its reply, and deny tools that change anything while a ping runs. |
| `prompt.edit`, `prompt.change`, `turn.complete` (main loop) | none | Remember when the prompt box was last touched or a turn last ended, so every injected prompt waits for a quiet box. |

Injected prompts (`task-notification`, `peer`, `plugin`, `sdk`, scheduled
triggers, Stop-hook continuations) are not returns.

The mod never sends a notification, reads Meridian config files or
`MERIDIAN_*` variables, writes under `~/.meridian`, or prints to the terminal.
Its own diagnostics live in the plugin's `$.store` under the key `log` (last
100 lines).

## `/meridian-idle`

Prints the current stretch, anchor, time left on each timer, the keepwarm state
and pings sent this stretch, and the last log lines. It does not count as a return.

## `/keepwarm`

Prints the keepwarm state. With no argument it toggles first; `on` and `off` set
it. It does not count as a return.

## State reminder

A lead that sits idle until its cache expires may leave its work item behind the
conversation. When the cache warning acts, core answers `fire warn` with
`remind: true` if `[idle] state_reminder` (default on; `[harness.claude.idle]`
overrides it) is on and the session has a work item. Core owns that decision, so
the mod reads no setting for it and takes `remind` as it comes. The mod then submits one
plugin prompt, `[meridian-idle state-reminder <nonce>]`, that asks the model to
update the work item's recorded state in place (`meridian work show`
lists it), replacing what is stale and writing nothing if it is current, and end its turn. Non-interactive and subagent sessions
never reach it.

It is sent like a ping, with the same guards: a quiet prompt box, the cold-cache
deadline, no running turn, no compaction, no user return (the quiet box, below,
spells them out). It is a plugin prompt
without `asUser`, so the model does not read it as the user, the session log
files it as injected, and it is not a return. It is the one prompt
here whose turn may write: the keepwarm tool denial applies to pings only, and
its reply ends with the model's choice (below).

With keepwarm on the reminder takes ping 1's slot (its turn warms the cache
too) and pings 2 and on follow as usual. With keepwarm off it is one unrequested
turn and takes no slot. Core claims the warning once per stretch, so the mod
keeps the reminder as owed in its own stretch record (`reminder` in `$.state`)
until it is submitted. The warn timer sends it; if a main-loop turn is running
then, or a guard holds it, the stretch's next prompt is the reminder instead of a
ping: the next arm schedules it one `lead` later, with keepwarm on or off, and a
reload recovers the stretch and does the same. The count and the flag are written
before the submit, so a reload cannot send it twice. The cold-cache deadline still
applies, and a stop, compaction or cold cache that ends the stretch's prompts
also drops it.

The reminder says its turn keeps the cache warm while the user is away, and asks
the reply to end with a last line that is exactly one of:

- `compact-now`: the work item holds what is needed to resume. The mod compacts
  the conversation while the cache is warm, so the user returns to a small
  context. The reminder says to choose it only after the update.
- `stop-waiting`: offered only with keepwarm on. Ends the stretch's prompts, as
  on a ping. The mod obeys it on any prompt; with keepwarm off there is nothing
  more to stop.
- anything else: keep waiting.

Pings do not offer `compact-now` and ignore it: they change nothing, so the
state the model weighed at the reminder is the state it would weigh again.
Without a work item core sends no reminder, so there is no compaction either.

On `turn.complete` of the reminder's turn (reason `answer`) with `compact-now`
the mod ends the stretch's prompts first (so no ping or chained timer races the
compaction, and a vetoed compaction does not restart them). It then waits like a
prompt does: the same quiet box, measured from the later of the last main-loop
turn's end and the last prompt change. That wait is what puts the call between
turns. It stops at the reminder's own cold-cache deadline (kept on the in-memory
prompt record, so a hot reload drops it and nothing compacts), a draft in the box
holds it until then, and another compaction pauses it. It gives up, with the
reason in the log, when the user came back (also during the reminder's turn) or a
main-loop turn is running (a task notification woke the session), before or after
the wait. Then it calls
`$.session.compact` with instructions to keep what the work item does not
hold (the user's latest requests, what is not yet recorded, where the
conversation stopped). The call runs outside the CLI queue, since it can take a
minute. The log ends `compacted`, `compaction skipped: <why>` (a hook vetoed it)
or `compaction failed: <error>`. The host runs `$.session.compact` through every
hook but the calling one, and on a real session the mod's own `session.compact`
hook did not see it (one "prompts are over" log line, not two); the prompts are
already ended either way. `state_reminder = false` turns all of this off.

## Keepwarm

Runs only in an interactive primary session. Starts at `[idle] keepwarm` (default
off; `[harness.claude.idle] keepwarm` overrides it), which arrives in the
`idle config` reply; the mod reads no config itself. A `keepwarm_pings` of 0 or
less arrives as `keepwarm: false` with the default count, so `/keepwarm on`
still works for the session. `/keepwarm on|off` (or a
toggle) overrides it for the session; the model cannot turn it on. A restart, a
resume or `/clear` drops the override (`$.state` is reset) and the configured
default applies again; a hot reload keeps it.

With it on, the cache warning (core's `warn` stage, `idle.warn_minutes` before
the cache expires) is followed by a prompt that keeps the cache warm: ping 1
when `fire warn` answers `act`, then up to `keepwarm_pings - 1` more. Core fires
the warning once per stretch, so the mod times the later pings itself: `lead`
(the gap core placed between the turn and the warning, taken from the arm that
placed it) after each later main-loop arm of the same stretch, and only once the
stretch's warning acted (`warned` in `$.state`; a warning core skipped starts no
pings). A new main-loop arm replaces the timer and a user return cancels it. A
stretch's record keeps the `limit` it started with (the resolved
`keepwarm_pings` at that moment, so a config change applies from the next
stretch), and `warned`; every way its pings end writes `sent = limit`, and the
count is taken before each submit, so a reload can cost a ping but never add
one. Pings send no notification.

- **Quiet box.** A prompt is submitted only after the prompt box has been empty
  and untouched, and no main-loop turn has ended, for 10 s. Every check before the submit stops 1 minute before
  the cache would expire: the stage's scheduled time plus `warn_minutes` (from
  `idle config`) minus 1 minute. The deadline does not move when the timer runs
  late, so a late timer has less time or none. A skipped prompt spends no slot.
  When that deadline passes while a prompt waits, the stretch's prompts end too
  (the deadline is the cold-cache check), so a later arm schedules none.
- **Not the user.** The ping is a plugin prompt without `asUser`, so the model
  reads it as sent by the plugin, and the mod does not count it as a return.
- **Its turn.** The text carries a nonce; the main-loop `turn.start` that
  carries it names the ping's `turnId`, and only that turn's `turn.complete`
  with reason `answer` is read.
- **A running turn.** If the warning or a chained timer fires while a
  main-loop turn is in flight, nothing is sent and no slot is spent: that turn
  refreshes the cache, and its own arm sets the next timer (an owed reminder or the next ping) one lead
  later (the stretch is already `warned`). A user return cancels it as usual.
- **Tools.** While the ping's turn runs, main-loop tool calls that are not
  `isReadOnly` are denied. Subagent loops and the rest of the session are not
  touched.
- **Stop.** A reply whose last non-empty line is `stop-waiting` (alone or in one
  pair of quotes or backticks; a reply that is only that word counts) ends this
  stretch's prompts; keepwarm stays on. The reminder's reply is read the same way. The ping
  text and the denied-tool reason both say so, and that stopping lets the cache
  go cold before the user returns.
- **Compaction.** A `session.compact` of the main conversation (trigger
  `manual`, `auto` or `plugin`, no `agentId`) during an armed stretch ends its
  prompts the same way and cancels the chained timer: the cache they protected is
  gone, and a ping would prefill the new summary and add a turn. `precompute`
  and a subagent's compaction change nothing. A manual `/compact` already counted
  as a return.
- **Your-turn push.** The ping turn's arm sends the last answer from a
  non-ping turn as `--assistant-text`, so the next notice does not read "ok".
  The answer is held in memory only: after a hot reload the next ping turn's
  arm has none, and that notice carries no assistant excerpt.

The mod keeps no record of the pings beyond its diagnostic `log`. Each ping is
in the session transcript, as a plugin prompt starting
`[meridian-idle keepwarm <nonce>]`, with the model's reply after it.

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
