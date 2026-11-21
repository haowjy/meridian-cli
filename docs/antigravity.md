# Using Meridian from Antigravity

Meridian never starts Antigravity (`agy`). You start `agy` yourself; Meridian installs a
plugin that lets the session spawn agents on other harnesses, wait for them, and receive
messages from other sessions. Meridian cannot stop `agy`. It reads two things of `agy`'s
own: the conversation's transcript, to see whether a queued message reached the model, and
the `subagents` folder of its conversation store, to tell a native subagent from a session.
Nothing else of `agy`'s is read, and its sessions can't be opened with `session log`.

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

## What the session can do

- **One tool, `meridian`.** It takes the arguments after `meridian`: `["spawn", "-m",
  "claude-haiku-5-5", "-p", "..."]`, `["spawn", "wait", "p1"]`, `["spawn", "list"]`. The
  agent finds commands with `["-h"]`. It runs outside `agy`'s shell sandbox, so children
  start as they would from your own terminal. `agy` asks you before the first call in a
  conversation (once, for the conversation, or always); allowing it allows every Meridian
  command for that conversation.
- **Meridian's guidance, as other harnesses get at launch.** `SessionStart` injects the
  skills index, the capabilities, the spawn rules and prompting guidance, worded for the
  `meridian` tool (one command per call, no shell, `spawn wait` blocks the turn). The
  session's own chat ID, spawn ID and work and task links follow in one more message at the
  first model call after its row exists, so they are missing from the first prompt. Both are
  built at run time by the same builders a launch uses, so they never go stale. The hook
  payload names no agent, so the session is treated as able to spawn and `meridian spawn
  subagents` lists what it may start. Native subagents get nothing.
- **A row in `spawn list --primary`.** The first prompt of a conversation registers it;
  the row stays running until `agy` exits. Children the session spawns are its children,
  and a no-argument `spawn wait` works.
- **Messages.** `meridian spawn message <spawn or chat> --message "..."` to the session
  prints `queued`: `agy` has no way to push a message in, so it reaches the model at the
  session's next model call, in the middle of a turn or at the start of the next one.

Native `agy` subagents are not sessions: they don't appear in `spawn list`.

## What it can't do

- **Wake an idle session.** A queued message waits until the model next runs, which
  means until you type or the current turn makes another model call.
- **Show the tool on the first prompt.** `agy` often offers the tool only from a
  conversation's second prompt, and the session's chat ID arrives with it. Until then the
  model has the guidance but may find no tool.
- **Cancel, continue, fork or read it.** `spawn cancel` on an `agy` session is refused
  ("this session runs in agy; end it there"), `--continue`, `--fork` and `--from` refuse an
  `agy` chat, `session log` finds no transcript, and `meridian agy` and `harness: agy` in a
  profile don't exist. Resume a conversation in `agy`; Meridian reopens the same chat for it.
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
