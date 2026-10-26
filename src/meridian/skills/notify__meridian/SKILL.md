---
name: notify__meridian
description: When a human decision, blocker, costly uncertainty, or finished task needs attention.
metadata:
  mode: primary
---

# Notify

For a decision the human must make, a costly uncertain call, a blocker, or a finished
task, run `meridian notify "<title>" "<body>"`. The title is one line that leads with
what they need to decide or see; the body is optional detail. Meridian adds the tmux
session, agent, and work item (or project) itself, so leave those out. Attach material
already on disk with repeatable `--attach <path>` instead of pasting it. Send each
notice once; routine progress is not a reason.

Examples, uncertain calls, `--no-email`, and `--priority`: `resources/examples.md`.
Route results, `--json`, and no-delivery fallback: `resources/delivery.md`.
