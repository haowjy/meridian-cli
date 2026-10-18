# Idle Policy Core

This package owns idle timelines, ordered stage guards, stretch transitions,
notification decisions, and the launcher-hosted sensor loop. Idle does two
things: the "your turn" push and the cache-expiry warning. Meridian does not
compact; harnesses compact on their own. It is policy, not a harness adapter:
**adapters report facts; core decides**.

## Invariants

- Never name or branch on a harness here. Harness-specific sensing, TTL
  detection, and environment facts belong behind the contracts in
  `../harness/idle_types.py`.
- `service.py` is the only transition API. Preserve at-most-once stage claims,
  anchor-versioned timers, and explicit user-return priority.
- Keep `timeline.py`, `guards.py` and `wake.py` pure. Guard order is behavioral:
  the first match is the reported reason.
- Adapters report the work a turn left running as `BackgroundTask` facts, in the
  host's own terms mapped to a kind. `wake.py` alone reads them, including
  Meridian's own CLI grammar. Only a foreground spawn creation or `spawn wait`
  that ends the shell is certain; loops, detached spawns, management commands,
  and commands followed by more work are maybe. An explicit wait `--timeout`
  (minutes) replaces the yield deadline. Never classify spawn commands in an
  adapter. A certain wake holds the push, and the response stays owed
  (`push_owed_since_ms`) until a push is delivered or the user returns. A known
  wake before cache expiry drops the warning.
- A harness reports that it needs the human through `IdleService.human_needed`.
  Only a push currently held by certain running work is released: re-anchor it
  at `push_seconds` and preserve the cache-warning deadline. No stretch, a
  closed stretch, a done push, or a push not held by work is a no-op. Adapters
  never decide whether to release a push.
- Authoritative state lives in `../state/idle_store.py`; sidecar timers are
  disposable and reconstructed from that state.
- A primary reopen through Meridian closes any open stretch in the launcher
  before exec. Keep this out of exit hooks (a killed terminal runs none) and
  adapters (engines cannot distinguish reload from resume, and reload recovery
  must keep working).
- Inject notification delivery. This package constructs `lib/notify.Notice`
  values and depends on `lib/notify` in the designed one-way direction.
- The sidecar is hosted in the primary TUI process. It must contain sensor
  failures, move synchronous service/store work off the launcher event loop,
  and stop before the live connection is torn down. Its liveness follows the
  TUI launch task, not the backend observer stream. Use stdlib logging for
  library diagnostics and the spawn-dir `DebugTracer` for sensor failures;
  never use structlog or write to stderr.

## Boundaries

- CLI argument parsing and human rendering belong in `cli/`; adapter result shapes
  and their wire serialization stay with the service result types.
- TTL detection reads the chat's launch-recorded native store through the
  session store using `MERIDIAN_CHAT_ID`. Never pass or derive a cwd for
  transcript lookup: agents can `cd`, while the transcript remains filed under
  the launch directory.
- A sensor may attach `IdleEvent.provider`; core forwards it to the harness
  `detect_ttl` port without interpreting provider names.
- Disk layout, locks, atomic writes, and garbage collection stay in
  `../state/idle_store.py`.
