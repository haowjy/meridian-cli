# lib/notify/ — Context

## Rationale

Notification delivery is a leaf service, modelled on `lib/artifact/` rather than
`lib/ops/`: sending a message touches no spawn, session or work policy, so it
stays independent of idle. `meridian notify` works with idle turned off, agents
and spawns can call it directly, and `lib/idle` depends on this package, never
the reverse. Policy (which backends a notice goes to, deduplication) lives in
`service.send()`; channels only transport, and failures come back as data in
`service.SendReport` so one broken backend cannot block another. Hook-route results
are aggregated separately by `emit.RouteReport`. Full record: KB
`decisions/idle-notifications.md` (D-idle-leaf-packages,
D-notify-smtp-password-file, D-notify-from-spawns, D-notify-hook-events).
