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
For spawn state, prefer v2 state helpers (`create_lifecycle_service`,
`spawn_store.get_spawn`) or direct `state.json` assertions.

### Pi Fake Fidelity

`tests/support/pi.py` provides `FakePiConnection` and `PiDrainScenario`. Real Pi
RPC connections emit an `error/connectionClosed` event with the Pi subprocess exit
code before the event iterator reaches EOF. Test fakes must model this when testing
exit classification. Use the fidelity helpers:

- `pi_process_exit_event(return_code)` — builds the `error/connectionClosed`
  event with the canonical `Pi subprocess exited with code <N>.` message.
- `write_pi_bash_record(runtime_root, spawn_id)` — writes managed-bash disk
  evidence for Pi private work that is not a Meridian spawn row.

Do not test Pi exit classification using only clean iterator exhaustion or direct
`handle_stream_exit(None)` — that shape avoids the real precedence path where the
generic process-exit failure arrives before stream exit.
