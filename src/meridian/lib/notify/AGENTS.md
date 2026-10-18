# lib/notify/ — Notification Delivery

Harness-agnostic labels and one-shot delivery transports. The message is
`plugin_api.Notification` (title, body, priority, email, label dict,
attachment paths); nothing here defines another. Callers build one
(`notice.manual_notification`, `lib/idle/messages.stage_notice`) and call
`emit.emit(event, notification, ...)`, which fires that hook event through
`lib/hooks`; the routes on it deliver it. The built-in `notify` hook calls
`service.send(notification, cfg, event=..., channels=...)`, and `emit` turns
each route's `HookResult` into one `SendResult` named by the hook. Never call
`send()` from a caller: that skips routing and every extension. Nothing here
retries or reads notification config from environment variables directly.

`emit.py` is the only module that imports `lib/hooks`, lazily.
If hook rows cannot load, `emit` logs the error and fires the event through
only the default `notify` routes. This keeps idle delivery fail-open; CLI log
rendering still shows the error from `meridian notify`, and hook inspection
raises it normally.

## Channel seam

Each backend is one module under `channels/` exposing a `CHANNEL` with
`send(notification, cfg, *, event) -> SendResult`. Add its entry to
`channels.REGISTRY`, the only registration point; mark email transports
`email=True`, and `send()` skips them for a notification with `email=False`
however a route named them. A route's `channels` option names backends
directly or through `push` / `email` (the configured backends). User-defined
channels are command hooks on the notification events, not backends. A
backend failure is data in its result and must not stop another selected
backend.

## Output and logging

This package never writes stdout/stderr directly. Expected transport failures
and warnings travel in `SendReport`; CLI callers render them through the CLI
output sink, while in-process callers decide where they belong. The one logger
warning is the hook-row load error in `emit`: idle otherwise has no diagnostic,
and CLI logging already shows it, so it is not duplicated in `SendReport`.

SMTP credentials come from `smtp_password_file` first, and that file is refused
when group/world readable; `MERIDIAN_NOTIFY_SMTP_PASSWORD` is only a documented
fallback. `MERIDIAN_SECRET_*` variables are stripped at the child boundary in
`lib/launch/env.py`, while the plain fallback reaches every spawn and can land
in transcripts, so prefer a mode-0600 password file.
