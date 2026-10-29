# dev/ — Contributor Tooling

Developer-only utilities, not part of the CLI surface: `pytests.py` (`pytest-llm`
entry point), `preflight.py` (concurrent fast-gate runner behind
`scripts/preflight.sh`) and `lints.py` (structural rules about the source tree).

`uv run pytest-llm` runs the default paths with trimmed output; the full suite is still
`uv run pytest tests/`. The wrapper trims output
(`--tb=line`, `--maxfail=1`). `PYTESTS_LAST_FAILED=1` adds `--lf --lfnf=all`.

## lints

`python -m meridian.dev.lints` (also `-m meridian.dev.preflight`) rejects source-tree
shapes no test should assert: a raw file write outside the atomic write contract, a
`MERIDIAN_*` literal the env registry does not own, a harness adapter importing
`meridian.lib.idle`, a per-launch file name written as a literal or a `LaunchPaths`
construction, alias, copy, `reserved` or `preview` call outside its owner (`state/paths.py`,
the Pi `shared/pi_state_paths.ts`, `state/spawn_store.py`, the dry-run entry points), a
private `paths.py` name imported elsewhere. Each rule is one function over `Sources` from `read_sources()`,
returning `Violation`s that carry a path relative to the scanned root and a sentence
naming the rule; `main()` prints them and exits 1. Keep the allowlists and their
justification strings.

The step is read-only, deterministic and runs before the tests in both gates
(`scripts/preflight.sh` fast and full), so keep it under a couple of seconds: a rule may skip modules that don't mention its marker
(`PythonSource.mentions`) only when the marker is an identifier token, which the source
must spell as written. A rule over a string value or a dotted name parses every module:
implicit concatenation, escapes, spaces and line continuations change the text without
changing the AST (the comment above `_RAW_WRITE_MARKERS` in `lints.py`).
`tests/unit/dev/test_lints.py` plants one violation per rule in a temporary tree, so a
rule that stops firing fails the suite.
