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
- Context baselines survive returns and new stretches. Only a trustworthy shrink
  or the first real turn after Meridian's own successful compaction resets them;
  absorbed compaction turns never update context. The first arm after a closed
  stretch with claimed/successful compaction also skips observation: it may be
  the compaction's own late turn end.
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
- Spawn-store reads for the child guard go through `children.py`, stay read-only,
  and use derived status so a dead unfinalized child does not block idle.
- TTL detection reads the chat's launch-recorded native store through the
  session store using `MERIDIAN_CHAT_ID`. Never pass or derive a cwd for
  transcript lookup: agents can `cd`, while the transcript remains filed under
  the launch directory.
- A sensor may attach `IdleEvent.provider`; core forwards it to the harness
  `detect_ttl` port without interpreting provider names.
- Disk layout, locks, atomic writes, and garbage collection stay in
  `../state/idle_store.py`.
