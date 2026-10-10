# dev/ — Contributor Tooling

Developer-only utilities. Not user-facing. Not shipped as part of the CLI surface.

## What's Here

- `pytests.py` — `pytest-llm` entry point: token-efficient pytest wrapper with `--tb=line`, `--maxfail=1`, `--lf` (last-failed)
- `preflight.py` — prepared-environment concurrent fast gate runner with a
  60-second warning target and process-group cleanup on failure/interruption.

## Usage

```bash
uv run pytest-llm          # run tests (token-efficient output)
PYTESTS_LAST_FAILED=1 uv run pytest-llm   # re-run only last-failed tests
scripts/preflight.sh extended  # explicit full suite/build gate
```

`PYTESTS_LAST_FAILED=1` triggers `--lf --lfnf=all`. Never use `uv run pytest` directly — the wrapper exists to reduce token noise in agent output.
