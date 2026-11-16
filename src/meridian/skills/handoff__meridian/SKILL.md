---
name: handoff__meridian
description: When a handoff brief must launch a new interactive Meridian lead.
metadata:
  mode: primary
---

# Handoff Launch

Print the command that launches the new lead from its brief. The user starts the new
primary session; never run the command yourself.

If no brief exists, write one in the work directory's `prompts/` (a temporary file
without a work item): the goal, what the lead owns and what stays with you, the current
state, decisions and why, open questions, and the artifact paths to read first.

Choose the requested agent; if the choice is ambiguous, list agents with
`meridian mars list --kind agents` and ask. Print a root primary launch, not
`meridian spawn`, in this shape:

```bash
meridian -C /absolute/project -a AGENT \
  --work WORK_ID \
  --task-dir /absolute/source/checkout \
  --prompt-file /absolute/work/directory/prompts/handoff.md \
  --from cNNN
```

Write every reference as its concrete value because the user's new terminal does not
inherit this session's shell variables. `-C` is the project that holds the chat and
work IDs; `--task-dir` is the source checkout, when it is a worktree or sibling checkout.
Pass your chat ID as `--from`: `$MERIDIAN_CHAT_ID`, which a restricted shell can't
expand, so copy it from your capabilities block. The new lead's prompt then lists how
to catch up on this session: its work item, the end of its log, search.

Chat and spawn IDs count per project, so under `meridian -C <other project>` a bare
`cNNN` names nothing or a different chat. For a handoff to a lead in another project,
leave `--from` and `--work` out (neither resolves there) and have the brief name the origin with
`meridian -C <origin project> session log cNNN --tail 20`.

`--from` attaches the new lead to this session's work item; `--work` names another
existing item instead. A lead on separate work has no item yet: leave `--work` out, and
have the brief tell it to start its own with `meridian work start`.
