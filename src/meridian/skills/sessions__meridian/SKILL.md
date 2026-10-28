---
name: sessions__meridian
description: When decisions or context must be recovered from past sessions.
---

# Session Mining

Design decisions, rejected alternatives, and discovered constraints often live in
conversation instead of code. Compaction makes them expensive to recover later, so mine
before the next implementation or documentation step starts. Skip mining when your
brief already carries the rationale; otherwise skim current artifacts and mine only
the gaps.

## Start at Your Launcher

`$MERIDIAN_CHAT_ID` is the chat of the conversation that launched you: your own chat
if you are a primary session, your parent's chat if you are a spawn, so it is not the
root at depth two or more. A primary's capabilities block gives its `cNNN` value; a
spawn's `# Meridian run` block gives its launcher's. Use that literal in restricted
shells. To reach the root, walk up: `meridian spawn show <spawn ID>` prints a `Parent`
line, so repeat on each parent until it has none; the last spawn's chat is the
root's. Pass any chat or spawn ID you find to `session log`. If the chain is cut
or the brief names a different session, ask your caller. Begin with recent context:

```bash
meridian session log cNNN --tail 20
```

Long sessions are split into segments at each compaction; `meridian session log -h`
covers windows, segments, and full content. Keep the `--segment N` that `Open:` and
`Next:` lines print: a position without it counts inside the current segment, which
changes at each compaction. Use `--global` only for one flat read across
segments. A segment starts with its setup slot (entry 0), then the opening brief or
compaction summary; read both with `--segment N --from 0 --limit 2`.

Search when you know the term or decision:

```bash
meridian session search "decision text" cNNN
meridian session search "auth" --work WORK_ID
meridian session search "shape" --workspace
```

Run the deterministic `Open:` command printed for a hit instead of rebuilding a
window. For interrupted or multi-day work, list all related sessions first with
`meridian work sessions WORK_ID --all`.

## Delegate Bulk Reading

For long histories or multiple sessions, delegate synthesis to a session-mining agent
if one is listed, passing a prompt file with the question and scope. If you are that
agent, mine directly rather than delegating recursively. Read narrowly first, widen
only when the missing context requires it.
