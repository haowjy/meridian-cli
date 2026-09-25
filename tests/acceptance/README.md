# Exact model-intent acceptance

Run the fixed synthetic manifest in its own interpreter, never through pytest
collection. Use an **existing** matching environment; do not sync/install:

```sh
uv run --offline --no-sync --no-env-file python -I -S \
  tests/acceptance/run_session_model_intent.py \
  --source-root "$PWD" \
  --dependency-root "$PWD/.venv/lib/python3.14/site-packages"
```

Supply the dependency path for the interpreter actually used. The runner checks
the version and directories, sanitizes its environment and cwd, and installs a
permanent audit guard before importing dependencies or Meridian. It does not run
`site`, `.pth` files, pytest, plugins, native adapters, catalogs, or old-spawn reads.
The pytest integration bridge launches this same program and checks that the
parent environment is unchanged. Legacy lock/concurrency tests remain separate.

The guard denies process/exec, all sockets including UNIX, DNS, foreign-symbol
loading, and data access outside disposable roots. Self-tests use actual APIs
where possible; socket methods and `ctypes.dlsym` use their audit events because
creating the prerequisite socket/library is already forbidden. Counts distinguish
self-tests, dependency initialization and workload. Pinned psutil initialization
tries `/proc/stat` three times on Linux: all are denied, its OSError fallback is
used, and these denials are explicitly reported before workload starts. No proc,
user or native data is read. Other outside dependency reads fail the bootstrap.

This is a Python API safety boundary for trusted test/application code, not an OS
sandbox against malicious extensions. Guards remain active through cleanup.
The manifest reports exact binding/invalidation/freeze visits and journal
read/decode/fold work separately; it makes no wall-time performance claim.

## Continue replay acceptance

`run_continue_replay.py` uses the same isolated-interpreter command and permanent
effect/data guards, with a separate fixed manifest permitting launch/harness
imports. It runs C1 alongside C2's settings fold on fake exact content, the private
C3 collector, and real primary/spawn legacy callers with fake native/Mars/state
effects. No installed harness or catalog process runs. C1's standalone layer-import
guard is unchanged. Run `tests/integration/launch/test_continue_replay_isolated.py`
for the pytest bridge.

The admitted-source DTOs are synthetic downstream values, not transport proof.
The public tracked gates are verified separately by the primary/spawn suites.
The new pure APIs have no pre-fix failing implementation; this manifest establishes
their contract rather than claiming pre-fix regression evidence.
