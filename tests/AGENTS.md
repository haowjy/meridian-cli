# Testing

Prefer smoke tests over unit tests. Too many unit tests is bad when you're
constantly refactoring. Unit tests are for logic that's hard to smoke test —
signals, concurrency, security/env sanitization, sync engine algorithms, parsing
edge cases.

## Running

```bash
uv run pytest-llm              # Unit tests (token-efficient output)
uv run meridian                # Smoke test the CLI directly
```

## Where Tests Go

```
tests/unit/         Pure logic, no I/O. <2s total.
tests/integration/  Real filesystem, subprocesses, cross-module wiring.
tests/contract/     API shapes and type contracts that must not drift.
tests/platform/     OS-specific behavior (Windows vs POSIX).
tests/smoke/        Markdown guides for manual CLI verification.
```

## Available Fakes

`tests/support/fakes.py`: `FakeClock`, `FakeHeartbeat`.
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

## Module Reload and Class Identity

Integration tests that call `importlib.reload()` on a module create a new class
object for every class defined in that module. If a unit test sharing the same
xdist worker imported the class at collection time, `isinstance` and
`pytest.raises` compare against the stale class object and fail. Reference
symbols through the live module object instead of binding at import time. See
`tests/integration/launch/test_streaming_runner_watchdog.py` for the pattern.
