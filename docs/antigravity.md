# Using Meridian from Antigravity

Meridian never starts Antigravity (`agy`). You start `agy` yourself, with a command Meridian
prints (`meridian agy`, below) or your own; Meridian installs a plugin that lets the session spawn agents on other harnesses, wait for them, and receive
messages from other sessions. Meridian cannot stop `agy`. It reads two things of `agy`'s
own: the conversation's transcript (`transcript_full.jsonl`, under `~/.gemini/antigravity-cli/brain/<conversation>/`),
to show it and to see whether a queued message reached the model, and
the `subagents` folder of its conversation store, to tell a native subagent from a session.
Nothing else of `agy`'s is read.

## Install

Antigravity needs Meridian's Mars 0.38.0 or newer (`meridian --version` shows the build;
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
- **Refused, with the reason:** `--fork`, `--fork-fresh`, `--from`, `-f`, `--skills`, `--goal`,
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
  prints `queued`: `agy` has no way to push a message in, so it reaches the model at the
  session's next model call, in the middle of a turn or at the start of the next one.

- **Reading.** `meridian session log <chat or spawn>`, `session search` and `spawn --from <chat>`
  work on an `agy` chat as on any other: the whole conversation, including turns after you
  resumed it in `agy`. Messages Meridian injected show as injected messages (`--tools`).
  Google doesn't document the transcript format and it can change with any `agy` version
  (seen on `agy` 1.5.13 only). Entries Meridian doesn't recognise are skipped and the
  log says its rendering is incomplete.

Native `agy` subagents are not sessions: they don't appear in `spawn list`, and their
conversation IDs can't be read as sessions.

## What it can't do

- **Wake an idle session.** A queued message waits until the model next runs, which
  means until you type or the current turn makes another model call.
- **Show the tool on the first prompt.** `agy` often offers the tool only from a
  conversation's second prompt, and the session's chat ID arrives with it. Until then the
  model has the guidance but may find no tool.
- **Cancel or fork it.** `spawn cancel` on an `agy` session is refused ("this session runs in
  agy; end it there") and `--fork` refuses an `agy` chat. `meridian --continue <chat>` prints
  the command that resumes it.
- **Deliver to a session that ended.** A message that `agy` never showed its model is not
  delivered: the process exited first, or the transcript did not show it after three
  injections (the message is then not injected again). The sender is told, but late: the
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
(`<pid>-<birth>`) with a request file per conversation (written by the hook) and the row
the server holds, and a message inbox per conversation. A server that is killed leaves its
rows to recovery. See [`src/meridian/lib/harness/agy/`](../src/meridian/lib/harness/agy/)
for the hook and server, and [`src/meridian/mars/`](../src/meridian/mars/) for what Mars
installs.
