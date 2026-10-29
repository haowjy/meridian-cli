# Testing

Prefer smoke tests over unit tests; too many unit tests hurt when you're constantly
refactoring. Unit tests protect pure functional cores that are hard to exercise through
the CLI: policy and state transitions, concurrency decisions, security/env sanitization,
algorithms, and parsing edges.

Write few tests. Verify a change by running the CLI against a scratch runtime and running
the existing tests that cover what you touched. A new test, parameterized case, or
extension of an existing test must guard one of:

- a pure core whose edges the CLI can't reach cheaply (the list above);
- a regression your brief names, at a seam that reproduces the real bug;
- a contract an outside consumer relies on: a harness protocol, an on-disk format, a
  published CLI output shape.

Before adding one, extend or replace the test that already covers the same behavior, and
delete a weaker duplicate in the same change. Every test runs in every full gate, and
integration tests that spawn processes cost seconds each.

**No flaky tests.** A test that fails intermittently, in CI or under load, is made
deterministic or deleted. Never raise its timeout, add a retry or `flaky` marker, or skip it
under load. Before deleting one, temporarily break the product behaviour it guards, run the
full suite, and revert: if nothing else fails, it is the only coverage, so make it
deterministic instead. Deterministic means: a test whose outcome depends on elapsed time gets
its time from `support/fakes.FakeClock` (no local clock stubs); a wait polls the condition
under test with no deadline of its own (the 60 s pytest-timeout, `timeout_method = "signal"`,
is the hang guard); no fixed sleep before an assertion; no path shared between tests. A
flake can be a product bug: check the product before blaming the test.

**A rule about the source tree is a lint, not a test.** A check that reads files under
`src/` and asserts a structural property of them (import layering, raw file writes, an
unregistered `MERIDIAN_*` literal) runs no behavior, so it belongs in
`src/meridian/dev/lints.py` and the preflight `lints` step, not here. Reserving tests for
behavior keeps the suite's cost proportional to what regressions actually look like.
`tests/unit/dev/test_lints.py` is the guard for the lints themselves; add a planted case
there when you add a rule.

## Running

```bash
uv run pytest-llm       # default subset (prepared fast gate), not the complete suite
uv run pytest tests/    # full suite: run before marking a PR ready
uv run meridian         # smoke test the CLI directly
```

Focused paths override `testpaths` (including `tests/extended/`). `tests/smoke/` and
`tests/e2e/` are opt-in scripts and guides, not pytest-collected
([smoke index](smoke/README.md)). Default seams, Pi/Claude runtime-adapter gates, the
runner-history trap: [.context/CONTEXT.md](.context/CONTEXT.md). Changes under
`src/meridian/claude_runtime/` require the local `claude plugin validate`/`test` gates
there; CI lacks the `claude` binary.

## Where Tests Go

```
tests/unit/         Pure functional cores. <2s total.
tests/integration/  Real seams: filesystem, subprocesses, cross-module wiring.
tests/contract/     API payload/retry shapes and type contracts that must not drift.
tests/platform/     OS behavior: signals, process scope, encoding, locking.
tests/extended/     Expensive state and launch-lock lifecycle contracts; explicit paths only.
tests/smoke/        Opt-in scripts and guides for CLI-visible behavior.
```

Mock choreography around a real seam does not make a unit test: exercise the seam in
integration and stub only the external boundary (the one exception is in
[`unit/AGENTS.md`](unit/AGENTS.md)). Every new test directory gets an `__init__.py`,
or same-named files in two directories collide at collection.

## Test Support and Fakes

Search `tests/support/` before adding a local fake. For spawn state, prefer state helpers
(`create_lifecycle_service`, `spawn_store.get_spawn`) or direct `state.json` assertions.

- **Fakes must reproduce the real death shape.** A non-zero subprocess exit becomes a
  `meridian/error/connectionClosed` event before the iterator ends; don't shortcut with
  `handle_stream_exit(None)`. Native source fixtures carry faithful headers (never loosen
  the validator to pass a fixture). Detail and helpers: `.context/CONTEXT.md`.
- **Upgrade fixtures come from the real older build**, not state this build writes
  (`uvx --isolated --from meridian-cli==X meridian`; without `--isolated` uvx reuses the
  installed tool).
- **Tests that spawn the real CLI need a stub harness binary on `PATH`** (Mars validates
  harness installation even for `--dry-run`): `executables.prepend_fake_executables`.
- **Control-socket isolation must cross interpreter boundaries**: the root fixture's patch
  doesn't reach subprocesses; see `support/launch_lock_driver.py` and
  `integration/cli/test_spawn_report.py`.
- **`importlib.reload()` creates new class objects**, so a unit test on the same xdist
  worker that imported the class at collection fails `isinstance`/`pytest.raises`.
  Reference symbols through the live module (`integration/launch/test_streaming_runner_watchdog.py`).
