# lib/notify/ — Notification Delivery

Harness-agnostic leaf service for labels and one-shot notification delivery.
Callers construct a `Notice`; `service.send(notice, NotifyConfig)` fans out to
the selected push and email backends and returns a `SendReport`. It never retries
or reads notification config directly from environment variables.

## Channel seam

Each backend is one module under `channels/` exposing a `CHANNEL` that satisfies
`Channel.send(notice, cfg) -> SendResult`. Add its lazy entry to
`channels.REGISTRY`; that dict is the only registration point. A backend failure
is data in its result and must not stop another selected backend.

## Output and logging

This package never writes stdout/stderr. Expected failures and warnings travel
in `SendReport`; CLI callers render them through the CLI output sink, while
in-process callers decide where they belong. This matches sibling
`lib/artifact`: no library logger is used for user-facing diagnostics.

SMTP credentials come from `smtp_password_file` first, and that file is refused
when group/world readable; `MERIDIAN_NOTIFY_SMTP_PASSWORD` is only a documented
fallback. `MERIDIAN_SECRET_*` variables are stripped at the child boundary in
`lib/launch/env.py`, while the plain fallback reaches every spawn and can land
in transcripts, so prefer a mode-0600 password file.
