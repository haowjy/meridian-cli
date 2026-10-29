# dev/ — Contributor Tooling

Developer-only utilities, not part of the CLI surface: `pytests.py` (`pytest-llm`
entry point), `preflight.py` (concurrent fast-gate runner behind
`scripts/preflight.sh`), `lints.py` (structural rules about the source tree) and
`check_launch_paths.py` (a fast-gate source check: per-launch
file names appear only in `state/paths.py` and the Pi `shared/pi_state_paths.ts`, and
`LaunchPaths(...)` is constructed only in `state/paths.py`, `reserved` is called only by
`state/spawn_store.py`, and private `paths.py` names stay there).

`uv run pytest-llm` runs the default paths with trimmed output; the full suite is still
`uv run pytest tests/`. The wrapper trims output
(`--tb=line`, `--maxfail=1`). `PYTESTS_LAST_FAILED=1` adds `--lf --lfnf=all`.

## lints

`python -m meridian.dev.lints` (also `-m meridian.dev.preflight`) rejects source-tree
shapes no test should assert: a raw file write outside the atomic write contract, a
`MERIDIAN_*` literal the env registry does not own, a harness adapter importing
`meridian.lib.idle`. Each rule is one function over `Sources` from `read_sources()`,
returning `Violation`s that carry a path relative to the scanned root and a sentence
naming the rule; `main()` prints them and exits 1. Keep the allowlists and their
justification strings.

The step is read-only, deterministic and runs before the tests in both gates, so keep it
under a couple of seconds: a rule parses only the modules that spell its construct
(`PythonSource.mentions`), and the text of a module cannot miss a node its AST holds.
`tests/unit/dev/test_lints.py` plants one violation per rule in a temporary tree, so a
rule that stops firing fails the suite.
