# tests/ — Context

Lookup detail behind `AGENTS.md`.

## Running and gates

```bash
uv run pytest-llm              # Fast default subset (prepared fast gate)
uv run pytest tests/           # Full suite; run before marking a PR ready
uv run meridian                # Smoke test the CLI directly
```

Bare `pytest` and `pytest-llm` use the directory-level default paths in
`pyproject.toml` (unit, contract, platform, and selected integration seams).
Those seams are `integration/config`, `catalog`, `safety`, `harness`, `hooks`,
`idle`, `prompt`, `telemetry`, and `state`.
The default is intentionally not the complete suite. Use an explicit `tests/`
path for release, nightly, or full regression checks; focused paths continue to
override `testpaths`, including files under `tests/extended/`. `tests/smoke/` and
`tests/e2e/` are outside `testpaths`: they hold opt-in pytest modules (e.g. the
installed-`mars` argv check), scripts and manual/runtime guides. The
[smoke index](../smoke/README.md) distinguishes
cheap executable checks from live/network probes.

PR CI runs the full gate, but agents should still run `uv run pytest tests/`
before marking a PR ready. Tests marked `requires_pi_dist` skip locally when Pi
bundles are absent and fail on GitHub Actions; build them with
`cd src/meridian/pi_runtime && pnpm install && pnpm run build:extensions`.

Runtime adapter changes also have local package gates:

```bash
cd src/meridian/pi_runtime && pnpm run verify:extensions
claude plugin validate src/meridian/claude_runtime/meridian-idle
claude plugin test src/meridian/claude_runtime/meridian-idle
claude plugin validate src/meridian/claude_runtime/meridian-spawn-watch
claude plugin test src/meridian/claude_runtime/meridian-spawn-watch
```

The Claude commands are required for every change under
`src/meridian/claude_runtime/`; CI does not provide the `claude` binary. The Pi
gate builds the projected bundles and runs their Vitest suites; CI runs the Pi
Vitest suites in the `runtime-extensions` job.

Use `uv run pytest --runner-history=off <tests>` to trap implicit reads of legacy
spawn/artifact runner `history.jsonl` (Meridian no longer writes it). The trap is
inherited by CLI subprocesses through a test-only `sitecustomize`; legacy archive
inventory may still hash the bytes, and native Claude history is not trapped.


## Fake Fidelity

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

A fake connection that reports a native session ID must adopt
`spec.native_identity.session_id` in `start()` when the plan assigns
one. A fixed ID contradicts the prebound identity and fails the attempt as
`entry_mismatch` before the scenario under test runs. Fakes that model a missing
identity signal stay silent; report a different ID only in a deliberate
mismatch regression.

Native source fixtures for continue/fork tests carry faithful native headers:
Claude a first-line `sessionId`, Codex a `session_meta.payload.id`, Pi a
`type: session` header with `id` — each equal to the recorded ID. Exact
locators validate the header before resume, fork, or reads, so an empty or
`{}` journal is refused as `missing`, not resumed. Never loosen the validator
to make a fixture pass; fix the fixture.

Add fidelity only for a real behavior under test. Do not pre-build alternate close,
timeout, or reader-error scenarios without a contract they protect.

Upgrade fixtures come from the real older build, not from state this build writes.
`integration/state/test_history_schema_namespace.py` seeds `history.sqlite3` with tables captured
from 0.6.7. For a live upgrade probe, run the old build as `uvx --isolated --from
meridian-cli==X meridian`; without `--isolated`, uvx reuses an installed tool of
the same name and version, and "old" is the PR build. Confirm it is old before
trusting a result: 0.6.7 writes `spawns/pN/history.jsonl` and rejects
`--prune-runner-history`. Also run an old runner across the upgrade: a background
0.6.7 spawn that is still running while the new build runs.

## Caller Home Isolation

`tests/conftest.py` points `HOME` at an empty per-test directory and clears the native
store variables (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `OPENCODE_*`, `PI_CODING_AGENT_*`,
`XDG_*`). Launch code resolves native config and stores from those, so without the
isolation a test reads the caller's real `~/.claude` or `~/.codex` and passes or fails
by machine. A test that needs native config sets `HOME` (or the specific store
variable) itself, as `test_launch_process_claude_session.py` and
`test_owned_identity_shims.py` do.

## CI Environment

Tests that spawn the real CLI (e.g. `test_spawn_prompt_input.py`) need a stub
harness binary on `PATH`. Mars validates harness installation even for `--dry-run`,
so the test fails with `pre_init_failed` on CI runners that have no harness binaries.
Use `tests/support/executables.prepend_fake_executables` to inject stubs.

For a fake executable that logs argv or emits fixed output, prefer a POSIX
`sh` shim over a cold Python process. Starting the child does not prove the shim
reached its observable action: bounded-poll for the expected log or state before
teardown instead of sleeping or stopping immediately.

