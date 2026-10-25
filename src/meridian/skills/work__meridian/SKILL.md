---
name: work__meridian
description: When an effort needs a shared work item or owned lifecycle across sessions.
mode: primary
---

# Work Items

Small direct tasks can run without a work item. Start one when the effort needs shared
artifacts or tracking:

```bash
meridian work start "NAME"
```

Attach to an existing item with `meridian work switch NAME`; delegated agents inherit
the attachment. Point it at the source checkout with `meridian work task-dir PATH`, and
keep its phase visible with `meridian work update NAME --status PHASE`.
`meridian work -h` lists the read and management commands.

Handing work off for review is not finishing it: set a status such as `pr-open` and
leave the item active until the change is accepted and cleaned up. Then
`meridian work done NAME` archives it; if it warns that sessions are still attached,
detach with `meridian work clear`. Archive only when you own the whole lifecycle; to leave
an item you do not own, use `meridian work clear` alone.
