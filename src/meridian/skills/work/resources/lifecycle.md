# Owning a Work Item

## Start and Track

Small direct tasks can run without a work item. Start one when the work
needs its own artifacts or tracking, or attach to an existing work item when
continuing it. Delegated agents inherit the attachment. Point the work item
at the source checkout where edits happen.

Keep the current phase visible in the work item's status.

## Finish

Handing work off for review is not finishing it: set a status such as
`pr-open` and leave the item active until the change is accepted and
cleaned up. Then close and archive the work item. Do that only when you own
the whole lifecycle, not a slice of it; detach from an item you don't own.

## The Work Directory

Its `AGENTS.md` orients a delegated agent arriving cold: the goal, the artifact
layout, and what is settled versus still moving. Keep it current as the
work shifts, per `/qi-layer`.

Decisions scoped to one change go with it, in its commit message or a note
beside what it explains; structural decisions go in the work directory.
When the plan shifts during execution, log the shift in `DIVERGENCE/`.
Reviewers check alignment against it, and knowledge capture mines it.
