# Testing

Prefer smoke tests over unit tests. Too many unit tests is bad when you're
constantly refactoring. Unit tests protect pure functional cores that are hard
to exercise through the CLI: policy and state transitions, concurrency
decisions, security/env sanitization, algorithms, and parsing edges.

## Running

```bash
uv run pytest-llm              # Unit tests (token-efficient output)
uv run meridian                # Smoke test the CLI directly
```

## Where Tests Go

```
tests/unit/         Pure functional cores. <2s total.
tests/integration/  Real seams: filesystem, subprocesses, cross-module wiring.
tests/contract/     API payload/retry shapes and type contracts that must not drift.
tests/platform/     OS behavior: signals, process scope, encoding, locking.
tests/smoke/        Markdown guides for CLI-visible behavior.
```

Mock choreography around a real seam does not make a unit test. Exercise that
seam in integration and stub only the external boundary. The narrow exception
for a coherent fail-closed security suite is defined in
[`unit/AGENTS.md`](unit/AGENTS.md).

## Shared Test Support

Search `tests/support/` before adding a local fake. Common helpers include
`fakes.py` for clocks and heartbeats, `async_determinism.py` for bounded async
observation, `executables.py` for CLI preflight stubs, `process_race.py` for
cross-process contention, and adapter-specific support in `opencode.py`,
`pi.py`, and `resident_drain.py`.
For spawn state, prefer state helpers (`create_lifecycle_service`,
`spawn_store.get_spawn`) or direct `state.json` assertions.

### Fake Fidelity

When exit classification depends on event ordering, fakes must reproduce the real
connection's pre-EOF death shape. A non-zero subprocess exit is not clean iterator
exhaustion: the exit code becomes a `meridian/error/connectionClosed` event before the
iterator ends. This invariant applies to every adapter that synthesizes such an
event. Do not replace it with direct `handle_stream_exit(None)` calls; that skips the
precedence path where a generic process-exit failure is recorded before stream exit.

Worked helpers:

- `tests/support/pi.py`: `pi_process_exit_event(return_code)` builds Pi's canonical
  close event, and `write_pi_bash_record(...)` supplies managed-bash disk evidence.
- `tests/support/opencode.py`: `FakeOpenCodeProcess.exit(return_code)` makes backend
  death observable while the OpenCode event iterator is still active.

Add fidelity only for a real behavior under test. Do not pre-build alternate close,
timeout, or reader-error scenarios without a contract they protect.

## CI Environment

Tests that spawn the real CLI (e.g. `test_spawn_prompt_input.py`) need a stub
harness binary on `PATH`. Mars validates harness installation even for `--dry-run`,
so the test fails with `pre_init_failed` on CI runners that have no harness binaries.
Use `tests/support/executables.prepend_fake_executables` to inject stubs.

For a fake executable that logs argv or emits fixed output, prefer a POSIX
`sh` shim over a cold Python process. Starting the child does not prove the shim
reached its observable action: bounded-poll for the expected log or state before
teardown instead of sleeping or stopping immediately.

## Module Reload and Class Identity

Integration tests that call `importlib.reload()` on a module create a new class
object for every class defined in that module. If a unit test sharing the same
xdist worker imported the class at collection time, `isinstance` and
`pytest.raises` compare against the stale class object and fail. Reference
symbols through the live module object instead of binding at import time. See
`tests/integration/launch/test_streaming_runner_watchdog.py` for the pattern.
