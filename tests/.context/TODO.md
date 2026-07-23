# TODO — tests

## Re-tier misplaced integration tests out of tests/unit/

The unit suite takes ~2 minutes; target per `tests/AGENTS.md` is under 2s.
Six files in `tests/unit/` are integration tests by behavior (subprocesses,
real timing waits, filesystem state) and belong in `tests/integration/` or
need deterministic process-lifecycle fakes with injected clock/wait
semantics:

- `tests/unit/harness/test_pi_integration.py` (966 lines — subprocesses, timing races)
- `tests/unit/core/test_spawn_service.py` (634 — cancellation polling, ~44s)
- `tests/unit/harness/test_cursor_subprocess_connection.py` (485 — 11s/test)
- `tests/unit/harness/test_extract_opencode_report.py` (622 — mixes pure parsing with fs fallback)
- `tests/unit/ops/test_work_scope_resolution.py` (507 — overlaps integration suite)
- `tests/unit/launch/test_launch_context_env_work_scope.py` (503 — cross-module fs wiring)

The structural fix is a shared deterministic process-lifecycle fake, not
per-file patching. Investigation: spawn p5598; artifacts in the
workstream-roadmap work dir under `test-pruning/`.
