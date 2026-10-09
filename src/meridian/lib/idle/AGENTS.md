# Idle Policy Core

This package owns idle timelines, ordered safety guards, stretch transitions,
notification decisions, and the launcher-hosted sensor loop. It is policy, not
a harness adapter: **adapters report facts; core decides**.

## Invariants

- Never name or branch on a harness here. Harness-specific sensing, TTL
  detection, environment facts, and compaction mechanisms belong behind the
  contracts in `../harness/idle_types.py`.
- `service.py` is the only transition API. Preserve at-most-once stage claims,
  anchor-versioned timers, explicit user-return priority, and stretch-keyed
  compaction completion.
- Keep `timeline.py` and `guards.py` pure. Guard order is behavioral: the first
  match is the reported reason.
- Authoritative state lives in `../state/idle_store.py`; sidecar timers are
  disposable and reconstructed from that state.
- Inject notification delivery. The production sender imports `lib/notify`
  lazily so this package's dependency direction remains one-way.
- The sidecar is hosted in the primary TUI process. It must contain sensor
  failures, move synchronous service/store work off the launcher event loop,
  and stop before the live connection is torn down. Its liveness follows the
  TUI launch task, not the backend observer stream. Use stdlib logging for
  library diagnostics and the spawn-dir `DebugTracer` for sensor failures;
  never use structlog or write to stderr.

## Boundaries

- CLI translation belongs in `cli/`, not here.
- Spawn-store reads for the child guard go through `children.py` and stay
  read-only.
- Disk layout, locks, atomic writes, and garbage collection stay in
  `../state/idle_store.py`.
