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
  --from pNNN
```

Write every reference as its concrete value because the user's new terminal does not
inherit this session's shell variables. `-C` is the project that holds the spawn and
work IDs; `--task-dir` is the source checkout, when it is a worktree or sibling checkout.
Pass your spawn ID as `--from`: `meridian task-dir --json` prints it as `spawn_id` (a
restricted shell can't expand `$MERIDIAN_SPAWN_ID`). A chat ID would instead seed the new
lead with that chat's transcript from before a `/clear`.

`--from` attaches the new lead to this session's work item unless `--work` names another
existing item. When the lead's work is separate from this item, the brief tells it to
start its own with `meridian work start`.
