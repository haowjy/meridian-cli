# Testing

Prefer smoke tests over unit tests. Keep no new test the user didn't ask for
(`/testing`). A test you keep or propose protects a hard-to-reach pure core, a named
regression at its real seam, or an external contract. Reuse existing
coverage and remove weaker duplicates.

Make flaky tests deterministic or delete them; do not hide them with retries,
longer timeouts or load-dependent skips. Before deleting one, verify that another
test catches the behavior. Use `FakeClock` for elapsed time and poll for the
condition rather than sleeping before assertions.

Source-tree structural checks belong in `src/meridian/dev/lints.py`, not tests.

Tests that spawn the real CLI need a fake harness executable on `PATH`: Mars
checks harness installation even for dry runs. Upgrade fixtures must come from a
real older build (`uvx --isolated`), not the current build. Control-socket
isolation must cross interpreter boundaries; a parent fixture's patch does not.
After `importlib.reload()`, reference classes through the live module: reload
creates new class objects, so a previously imported class can break `isinstance`
and `pytest.raises` checks on the same worker.

[.context/CONTEXT.md](.context/CONTEXT.md) covers runtime adapter gates and fake fidelity.
