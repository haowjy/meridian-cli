# Delivery

`meridian notify` sends once through each configured route: normally push and email,
plus notification hooks. It never retries. `--json` returns one result per route under
its `channel` name; sent results name successful `transports`, and failed results name
the failed backends.

Read the output instead of treating exit 0 as delivery:

- `Notification sent via notify (ntfy).` means at least one route delivered. In JSON,
  at least one result has `status: "sent"`.
- A top-level `dropped invalid hook rows` warning is a configuration problem to report.
- A route warning beside a sent result is partial delivery. If text arrived but an
  attachment did not, do not resend the text; record the missing attachment.
- `Nothing delivered.` means every route was disabled, skipped, or selected a `none`
  backend. It exits 0 because delivery was intentionally inactive, but the human was
  not notified.
- Exit 1 means every attempted route failed and none delivered.

For either no-delivery outcome, do not loop or configure delivery yourself. Use the
harness's notification tool if it has one; otherwise put the notice at the top of the
record or your next message. Keep working on anything that does not await an answer,
and report which warnings or errors occurred. Never put credentials in notices.
