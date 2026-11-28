# Spawn lifecycle

One owner for a spawn's record transitions and cleanup, built on `lib/state`, which never
imports it back (`dev/lints.py`). Its state machine lives with the record:
[../state/spawn/.context/state-machines.md](../state/spawn/.context/state-machines.md).
Lifecycle and cleanup contracts: [.context/CONTEXT.md](.context/CONTEXT.md); cancellation:
[.context/cancel.md](.context/cancel.md).

- `SpawnApplicationService` (`service.py`) is the single writer of a spawn's terminal status:
  cancel, `complete_spawn`, `complete_execution` and `finalize_dead_launch`. Nothing else
  persists an outcome.
- Per-spawn ordering of cleanup, terminal write and hooks comes from the files: the
  launch-lock claim (`claim_dead_launch`) and the spawn row lock. Services are built per
  operation (`bootstrap.services.build_spawn_application_service_from_roots`), so an
  in-process lock would serialize nothing; do not add one.
- `lifecycle.py` dispatches hook events only after the row write succeeds. Hooks build on it
  (`hooks/dispatch.py` imports its event types); `bootstrap` wires the dispatcher in.
- `reaper.py` takes its service from `bootstrap.services`, the one import from above.
