---
name: handoff
description: When a prepared transition brief must launch a new interactive Meridian lead.
---

# Handoff Launch

Follow the transition brief already prepared for the next lead. The user starts the
new primary session; print the launch command, but never run it.

Choose the requested agent, or inspect available agents and ask if the choice is
ambiguous. Show the user the work item's primary-session trail first, so they see
which leads came before and where this handoff sits in the chain:

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
for a worktree or sibling checkout, the prepared brief's absolute path, and the current
root conversation ID as `--from`.
