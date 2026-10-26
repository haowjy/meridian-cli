---
name: handoff__meridian
description: When a transition brief must launch a new interactive Meridian lead.
metadata:
  mode: primary
---

# Handoff Launch

Print the command that launches the next lead from its transition brief. The user
starts the new primary session; never run the command yourself.

If no brief exists, write one in the work directory's `prompts/` (a temporary file
without a work item): the human's intent in their terms, the current state, decisions
and why, open questions, and the artifact paths to read first.

Choose the requested agent; if the choice is ambiguous, list agents with
`meridian mars list --kind agents` and ask. Show the user the work item's
primary-session trail first, so they see which leads came before and where this
handoff sits in the chain:

```bash
meridian work sessions --primary
```

Print a root primary launch, not `meridian spawn`, in this shape:

```bash
meridian -a AGENT \
  --work WORK_ID \
  --task-dir /absolute/source/checkout \
  --prompt-file /absolute/work/directory/prompts/handoff.md \
  --from cNNN
```

Write every reference as its concrete value because the user's new terminal does not
inherit this session's shell variables. Include `--work` when attached, `--task-dir`
for a worktree or sibling checkout, the brief's absolute path, and the current
root conversation ID as `--from`.
