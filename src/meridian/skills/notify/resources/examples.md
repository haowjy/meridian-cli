# Notify Examples

## Title and Body

The title is what a lock screen shows: lead with what the human has to do
or see. A few lines of options or a summary go in the body. Attach files
already on disk instead of pasting their contents.

- **Pick auth storage: sessions table or JWT.** Tradeoffs are in `decisions.md`.
- **PR #412 ready for review.** Retry backoff; all checks green.
- **Blocked: staging DB refuses connections.** Since 14:10; nothing else to do until it's back.
- **Review the new settings page.** Attach `screenshot.png` and `report.md`.

Weak titles make the human open the session to find out whether it matters:
"Update", "Done with phase 2", "A delegated run finished", "FYI". The title
should stand on its own.

## Uncertain Calls

When you kept going past a call you were unsure of, tell the human before
more work builds on it and reversing it gets expensive. Record the call
with the work item's decisions, then batch: at the next pause (a phase
lands, a delegated run returns), send one notice that names each open call,
what you chose, the main alternative, and where the record is.

For example: **2 open calls in `decisions.md`.** Kept the sync API (async
rewrite deferred); split billing out of users.

Calls you're confident in, or that are cheap to undo, need no notice.

## Urgency

Send push only when the notice can wait until the human next checks their
phone: an uncertain call or a finished task they were not waiting on. Raise
urgency only for a failure that costs something while it waits, such as a
broken deploy or a runaway job.

