# dev/

Developer tooling for meridian contributors. Not shipped as user-facing
functionality.

## Entry Points

- `pytests.py` — `pytest-llm` entry point; token-efficient pytest wrapper
  with `--tb=line`, `--maxfail=1`, `--lf` (last-failed) support

## Usage

`PYTESTS_LAST_FAILED=1` triggers `--lf --lfnf=all` (re-run only failed tests).
Invoked via `uv run pytest-llm` — not `uv run pytest` directly.
