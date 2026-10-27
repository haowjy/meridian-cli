# Idle Policy Core

Idle timelines, ordered stage guards, stretch transitions, notification decisions, and the
launcher-hosted sensor loop: the "your turn" push, the cache-expiry warning, and forwarding
a harness's still-waiting permission signal. Policy, not a harness adapter: **adapters
report facts; core decides**. Meridian does not compact.

## Invariants

- Never name or branch on a harness here; harness sensing, TTL detection and environment
  facts live behind `../harness/idle_types.py`. A sensor may attach `IdleEvent.provider`;
  core forwards it to `detect_ttl` uninterpreted.
- `service.py` is the only transition API. Preserve at-most-once stage claims,
  anchor-versioned timers, and explicit user-return priority.
- `timeline.py`, `guards.py`, `wake.py` stay pure. Guard order is behavioral: the first
  match is the reported reason.
- `wake.py` alone classifies `BackgroundTask` facts (including Meridian's CLI grammar);
  never classify spawn commands in an adapter. It skips top-level value flags through
  `lib/cli_args.py` `TOP_LEVEL_VALUE_FLAGS`, shared with `cli/bootstrap.py`: a new
  value-taking top-level flag goes there. Report only work that wakes the session when it
  ends, or the push is held for nothing.
- Authoritative state lives in `../state/idle_store.py` (disk layout, locks, atomic writes,
  GC stay there); sidecar timers are disposable.
- A permission notice is stateless: never read or write the idle store, alter a stretch,
  hold or release the push, or track an answer.
- A primary reopen closes any open stretch in the launcher before exec, never in exit hooks
  or adapters.
- Inject notification delivery: build `plugin_api.Notification` values and emit through
  `lib/notify.emit` (`STAGE_EVENTS[stage]`, `NEEDS_PERMISSION_EVENT`); the dependency is
  one-way.
- The sidecar must contain sensor failures, move synchronous service/store work off the
  launcher event loop, and stop before the live connection is torn down. Use stdlib logging
  and the spawn-dir `DebugTracer`; never structlog or stderr.
- CLI parsing and human rendering belong in `cli/`. TTL detection reads the native store via
  `MERIDIAN_CHAT_ID`, never a cwd.

[.context/CONTEXT.md](.context/CONTEXT.md) — rationale, wake semantics per harness.
