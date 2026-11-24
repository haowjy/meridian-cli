# Using Meridian from Antigravity

Meridian never starts Antigravity (`agy`). You start `agy` yourself, with a command Meridian
prints (`meridian agy`, below) or your own; Meridian installs a plugin that lets the session spawn agents on other harnesses, wait for them, and receive
messages from other sessions. Meridian cannot stop `agy`. It reads three things of `agy`'s
own: the conversation's transcript (`transcript_full.jsonl`, under `~/.gemini/antigravity-cli/brain/<conversation>/`),
to show it and to see whether a queued message reached the model, and
the `subagents` folder of its conversation store, to tell a native subagent from a session,
and the `executor_metadata` table of the conversation's SQLite database
(`~/.gemini/antigravity-cli/conversations/<conversation>.db`, opened read-only), to notice
that you cancelled a turn with Esc. Nothing else of `agy`'s is read.

## Install

Antigravity needs Meridian's Mars 0.39.0 or newer (0.38.0 for the plugin alone; `meridian agy` needs 0.39.1) (`meridian --version` shows the build;
`meridian mars --version` shows Mars).

```bash
meridian mars link agy     # once per project: adds the .agy target
meridian mars sync         # installs the hooks, the MCP server and Meridian's skills into .agy
agy --agent <profile>      # as usual
```

`mars sync` writes two entries into `.agy/`, named `meridian-session` (hooks) and `meridian`
(MCP server). Nothing else changes; remove them with `meridian mars unlink agy`. If
`meridian` is not on `agy`'s `PATH`, the hooks do nothing and `agy` works as before.

## Start `agy` the Meridian way

`meridian agy` builds the launch the way `meridian claude` does (Mars picks the model and
effort, the profile, the workspace directories, `--continue`) and prints the `agy` command
instead of running it, so you run it yourself:

```bash
meridian agy -m gemini-3.8-flash --effort high -a coder -p "fix the failing test"
# sh /home/you/.meridian/agy/launch/3f9c1a7b2d40.sh
eval "$(meridian agy -m gemini-3.8-flash)"    # run it in your shell
meridian agy --continue c12                   # resume a chat; keeps its model, agent and approval
```

- **`--from` rides in the prompt.** `meridian agy --from c12` puts the standard prior-session
  block into `--prompt-interactive`. With `-p` as well, the prompt is that block, a blank line,
  then `## User message` and your text. The block carries the source's report and pointers, not
  its conversation; `meridian session log c12` reads the conversation.
- **It writes nothing** except a script for a long command. A command over about 240 characters
  or with a newline (a multi-line `-p`) is written to `~/.meridian/agy/launch/<id>.sh` and
  stdout is `sh <path>`. Expect that for nearly every real command: the work directory, the
  archive and the knowledge base are workspace roots, and each adds a `--add-dir <path>` that
  pushes the line past the limit. A short one looks like `(cd /path/to/project && agy --model
  gemini-3.8-flash-high --agent coder)`. Scripts older than 7 days are removed on the next run. Stdout is only
  the command; the hint that `eval` runs it, and any warnings, go to stderr. `--json` prints
  `{command, argv, cwd, env, script, warnings}`, where `env` is only what the command sets.
  `meridian agy --dry-run` prints the usual dry-run report, plus the directory and environment the command sets.
- **The session registers when you run the command.** The plugin's hook and server create the
  row, as for any `agy` you start. The command sets `_MERIDIAN_AGY_AGENT`,
  `_MERIDIAN_AGY_PROJECT` (the directory it launches from) and, when it isn't the default,
  `_MERIDIAN_AGY_APPROVAL` (internal, don't export them) so the row knows the profile,
  project and approval; a continue replays the agent and approval and omits `--agent`, as `agy`
  keeps the conversation's own. `agy`'s hook payload names no launch directory: its `cwd` is the
  first of its sorted workspace paths. A plain `agy` run with several workspaces can therefore land
  in the wrong project; the printed command avoids it. An `agy --conversation <id>` you run yourself, without
  them, records no agent or approval, and the next `meridian --continue` replays that: it is
  what the resumed session ran.
- **Model and effort are one choice.** Mars folds `--effort` into the model slug (`-m
  gemini-3.8-flash --effort high` is `--model gemini-3.8-flash-high`), because `agy` fails when
  given a tiered slug and `--effort`. An effort the model's tiers lack moves to the nearest one
  with a warning. A continue always passes `--model`: `agy` resumes on your default model
  without it.
- **Directories.** The command starts in the project directory. Workspace and context roots,
  `--task-dir` and `--work`'s directory become `--add-dir`, except a directory with its own
  `.agy/` or `.agents/`: its hooks and MCP servers would run beside the project's, so it is
  skipped with a warning; skipping only keeps its `.agy/` and `.agents/` from loading, and the
  session can still read the directory by absolute path under `agy`'s own permissions. `--work` and `--task-dir` only add directories; they do not move the
  session or attach it to a work item, and `--work` must name an existing item.
- **Approval.** No `--approval` prints no flag: `agy` uses its own default. `--approval auto`
  prints `--mode auto`; `--approval never` and `--yolo` print `--dangerously-skip-permissions`
  (the TUI footer still reads `auto`). `--approval confirm` is refused: `agy --mode` takes
  `accept-edits`, `auto` or `plan`, none asks before every edit, and `agy` without a flag opens
  in auto.
- **Agent rules.** The agent's scoped `deny` and `ask` rules that `agy` can enforce print as
  one inline `--settings` value, on a start and on `--continue` (a continue reads the agent's
  current profile rules, so an edit to them applies). A command rule becomes a whole-word
  command prefix (`bash(git reset --hard)` blocks `git reset --hard HEAD`); an edit or read
  rule on a path or directory blocks `agy`'s file tools for it; the skill rules cover both
  skill directories. `meridian agy --dry-run` lists what each rule became and what `agy`
  does not catch. Meridian adds the agent's rules to the `deny` and `ask` lists `agy` would have used: the
  one in a `--settings` you pass after `--` (inline JSON or a file path), else the one in your
  `settings.json`. It prints one `--settings`, because `agy` uses only the last one and a list
  in it replaces the same list elsewhere; your other keys and lists, `allow` included, reach
  `agy` as they would without Meridian. A file that can't be read is reported: an unreadable `settings.json`
  leaves your lists out of the value; an unreadable `--settings` is passed as you gave it,
  without the agent's rules. `allow` is never written, and `ask` is left out under
  `--approval never` (`agy` ignores it there). Not caught: a command `agy` runs through a
  wrapper (`env`, a shell), rules with `$`, `..`, a glob or a wildcard inside the command,
  and tools other than `agy`'s file, command and MCP tools. An MCP deny is written under both
  names `agy` gives a server (`_agy_<server>` for one Mars installs, the bare name for one added
  with `agy mcp add`), so it also stops a same-named server of yours. These rules only apply to the command
  Meridian prints: `agy` run by hand does not apply them.
- **Refused, with the reason:** `--fork`, `--fork-fresh`, `-f` (reference files), `--skills`, `--goal`,
  `--sandbox`, `--approval confirm`, `--network*`, `--autocompact*`, `--timeout`, and `--append-system-prompt` after
  `--`. Arguments after `--` go to `agy`.
- **`spawn` never routes to `agy`** (`spawn --harness agy`, `spawn --continue <agy chat>`), and
  a profile's `harness: agy` without `meridian agy` falls through to another harness.
- **A chat open in another `agy`** is refused: close that `agy` first. There is no fork.

## What the session can do

- **One tool, `meridian`.** It takes the arguments after `meridian`: `["spawn", "-m",
  "claude-haiku-5-5", "-p", "..."]`, `["spawn", "wait", "p1"]`, `["spawn", "list"]`. The
  agent finds commands with `["-h"]`. It runs outside `agy`'s shell sandbox, so children
  start as they would from your own terminal. `agy` asks you before the first call in a
  conversation (once, for the conversation, or always); allowing it allows every Meridian
  command for that conversation.
- **Meridian's guidance, as other harnesses get at launch.** the first hook call of a
  conversation (`SessionStart` unless it was missed) injects the
  skills index, the capabilities, the spawn rules and prompting guidance, worded for the
  `meridian` tool (one command per call, no shell, `spawn wait` blocks the turn). The
  session's own chat ID, spawn ID and work and task links follow in one more message at the
  first model call after its row exists, so they are missing from the first prompt. Each is
  injected only when the model's current context lacks it: a conversation resumed in a new
  `agy` process keeps the copies it has, and one `agy` compacted gets both again at its next
  model call (the hook reads the transcript for the last compaction). Both are
  built at run time by the same builders a launch uses, so they never go stale. The hook
  payload names no agent, so the session is treated as able to spawn and `meridian spawn
  subagents` lists what it may start. Native subagents get nothing.
- **A row in `spawn list --primary`.** The first prompt of a conversation registers it;
  the row stays running until `agy` exits. Children the session spawns are its children,
  and a no-argument `spawn wait` works.
- **Messages.** `meridian spawn message <spawn or chat> --message "..."` to the session
  prints `queued`. `agy` has no way to push a message in, so it reaches the model at the
  session's next model call: later in the current turn, or when the session wakes. The plugin
  confirms it at the turn's end by finding it in `agy`'s transcript; one it cannot find is
  queued again, which wakes an idle session, up to three sends.
- **Waking for messages.** An idle session wakes by itself, about a second after a message
  arrives, while its Meridian watcher runs. The watcher is a background command,
  `meridian harness-watch agy ...`, that the model starts when the plugin's `Stop` hook asks
  it to at the end of a turn; the TUI shows it as `1 task(s)` and your prompt stays usable.
  The hook asks when no watcher runs. If the model or you decline twice in a row, it asks
  less often (after 2 more turns, then 4, up to every 32), and asks every turn again once a
  watcher has run. A session that is in the middle of a turn is never woken.

  The watcher runs outside `agy`'s terminal sandbox (`BypassSandbox`), because the sandbox
  hides `~/.meridian`. If it starts inside the sandbox anyway, it says so and exits, and the
  model starts it again. Starting it is a shell command, which the `meridian` tool approval
  above does not cover, so `agy` may ask before each start. With
  `"permissions": {"allow": ["command(meridian)"]}` in
  `~/.gemini/antigravity-cli/settings.json` (the rule `agy`'s own "always allow for commands
  that start with 'meridian'" writes), neither Auto nor accept-edits mode asked. Without it,
  expect a prompt at each start (seen once in accept-edits mode; Auto mode without the rule,
  and the narrower `command(meridian harness-watch)`, are untested).

  A conversation you leave with `/new` keeps its watcher until `agy` exits. A message to it
  is acted on there, out of sight; resume it with `agy --conversation <id>` to see the reply.

- **Reading.** `meridian session log <chat or spawn>`, `session search` and `spawn --from <chat>`
  work on an `agy` chat as on any other: the whole conversation, including turns after you
  resumed it in `agy`. Messages Meridian injected show as injected messages (`--tools`).
  Google doesn't document the transcript format and it can change with any `agy` version
  (seen on `agy` 1.5.13 only). Entries Meridian doesn't recognise are skipped and the
  log says its rendering is incomplete.

Native `agy` subagents are not sessions: they don't appear in `spawn list`, and their
conversation IDs can't be read as sessions.

## What it can't do

- **Wake without a watcher.** A message to a session whose watcher is not running waits
  until you type.
- **Wake after Esc.** After you cancel a turn with Esc, `agy` waits for you to type before it
  acts on a finished background command (seen on `agy` 1.5.15), so a Meridian message sent
  after Esc waits until you type; the watcher stays running. The idle push and the cache
  warning still go out after Esc: `agy` fires no hook for the cancel, so Meridian reads it
  from the `executor_metadata` table of the conversation's database. An Esc within about a
  second of any model call starting can be missed; the notices then wait until the next turn
  ends.
- **Show the tool on the first prompt.** `agy` often offers the tool only from a
  conversation's second prompt, and the session's chat ID arrives with it. Until then the
  model has the guidance but may find no tool.
- **Cancel or fork it.** `spawn cancel` on an `agy` session is refused ("this session runs in
  agy; end it there") and `--fork` refuses an `agy` chat. `meridian --continue <chat>` prints
  the command that resumes it.
- **Deliver to a session that ended.** A message that `agy` never showed its model is not
  delivered: the process exited first, or the transcript did not show it after three
  sends (the message is then not sent again). The sender is told, but late: the
  notice goes out when the next `agy` server starts or the next primary session starts,
  in any project, not at the moment the session ends. A sender that has finished by then
  gets nothing, and so does one with no Meridian session. A notice can repeat.
- **Run a command with no session to run as.** If the conversation has no row (the
  hook did not register it, or another `agy` process holds it), the tool returns an error
  that says why instead of running the command. Close the other `agy` process or send
  another prompt, and call again.

## Terms

Google's terms for Antigravity on a personal sign-in forbid using `agy` as another
vendor's agent's worker. Messages from any session reach an `agy` session, so a Claude or
Codex session that sends an `agy` session tasks is using it that way. Your own typing
and `agy`'s own children are not. Meridian does not warn at launch or refuse such
messages.

## Reference

State is under `~/.meridian/agy/`: one directory per running `agy` process
(`<pid>-<birth>`) with a request file per conversation (written by the hook), the row
the server holds, and the watcher's per-conversation files (`busy/`, `asked/`, `watch/`,
`backoff/`), all removed with the directory once that `agy` process is gone; and a
message inbox per conversation. A server that is killed leaves its
rows to recovery. See [`src/meridian/lib/harness/agy/`](../src/meridian/lib/harness/agy/)
for the hook and server, and [`src/meridian/mars/`](../src/meridian/mars/) for what Mars
installs.

## Idle notices

Like other harnesses, an agy session gets a push notice when it has been waiting for you:
Meridian sends it some time after the agent's turn ends (the usual timing, which depends on
how long you waited for that turn), and typing resets that timer. A turn that Meridian's
watcher or an `agy` background command's exit started on its own does not reset it and sends
no second notice. You also get the cache warning before the prompt cache expires: Meridian
assumes a 30-minute cache and warns `idle.warn_minutes` (10 by default) before it ends, so the
warning comes about 20 minutes after the turn. Google publishes no cache window, and it depends
on the model: in November 2026 `gemini-4-neon` at Medium effort still hit the cache after
55 minutes idle, while `gemini-3.8-flash-low` had lost it by then. Set `ttl_seconds` below to
fit the model you run, such as `3600` for Neon Medium, and `warn_minutes` for the lead. Meridian reads each turn's end from the plugin's server; nothing is typed into
your terminal. When the warning goes out and the session has a work item, Meridian also queues
a message asking the model to bring the work item up to date (`state_reminder`, on by default);
it wakes an idle session like any message. With `keepwarm = true` (off by default) the
warning is followed by up to `keepwarm_pings` Meridian messages that keep the cache warm, one
lead apart; each costs two model calls on your quota, only goes out when the session can be woken for it
(the watcher runs and no turn is running or left open by an Esc), and ends when you type or the model's reply ends with `stop-waiting`. A typed `/compact` counts as
you typing and starts no new stretch, and `/clear` ends the old conversation's stretch; pings
and reminders resume after the next model turn you start. Known gaps: unlike Claude's
ping, agy's ping turn is not kept read-only (the hook cannot tell which tools only read), so a
model that acts on one runs an ordinary turn, and a ping can arrive while you are typing. To configure it, see
[`[harness.<harness>.idle]`](configuration.md#harnessharnessidle) with `agy` as the harness:

```toml
[harness.agy.idle]
enabled = true        # false turns off the push and the warning
ttl_seconds = 1800    # your estimate of your model's cache lifetime (the default)
warn_minutes = 3      # warn this long before it ends; overrides [idle] warn_minutes (10)
state_reminder = true # ask the model to update the work item at the warning
keepwarm = false      # true: keep the cache warm with Meridian messages after the warning
keepwarm_pings = 3    # at most this many per stretch (the reminder takes the first)
```
