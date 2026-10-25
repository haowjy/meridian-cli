---
name: sessions
description: When decisions or context must be recovered from past sessions.
---

# Session Mining

Design decisions, rejected alternatives, and discovered constraints often live in
conversation instead of code. Mine only the gaps after skimming current artifacts.

## Start at the Root

`$MERIDIAN_CHAT_ID` identifies the root conversation, regardless of delegation depth.
The capabilities block gives its concrete `cNNN` value; use that literal in restricted
shells. Begin with recent context:

```bash
meridian session log cNNN --tail 20
```

Long sessions are split into segments at each compaction; `meridian session log -h`
covers windows, segments, and full content.

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
