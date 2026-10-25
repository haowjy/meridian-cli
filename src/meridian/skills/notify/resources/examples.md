# Notify Examples

## Title and Body

The title is what a lock screen shows. Put a few lines of options or summary in the
body. Email carries every attachment; a phone push carries one per message.

- `meridian notify "Pick auth storage: sessions table or JWT" "Tradeoffs in decisions.md"`
- `meridian notify "PR #412 ready for review" "Retry backoff; all checks green"`
- `meridian notify "Blocked: staging DB refuses connections" "Since 14:10"`
- `meridian notify "Review the settings page" --attach screenshot.png --attach report.md`

Weak titles such as "Update", "Done", or "FYI" make the human open the session just
to learn whether the notice matters.

## Push Only and Priority

A notice normally goes to push and email. Add `--no-email` when it can wait until the
human next checks their phone: for example, an uncertain call or a finished task they
were not waiting on. `--priority` runs from 1–5 (default 3); raise it only for a failure
that costs something while it waits, such as a broken deploy or runaway job.

## Uncertain Calls

When continuing past a costly uncertain call, record it, then batch open calls at the
next pause. Name what you chose, the main alternative, and where the record is:

`meridian notify --no-email "2 open calls in decisions.md" "Kept sync; split billing"`

Confident calls and cheap-to-undo choices need no notice.
