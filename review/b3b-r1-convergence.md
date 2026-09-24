# R1 convergence test correction

## Verdict: P2 closed; R1 test expectations now match the accepted leaves

Corrected the two stale owner expectations without changing production policy:

- Codex managed `-c tools.web_search=true` is treated as unproved, and the owner
  test now asserts a redacted refusal after exactly one fake ordinary route and
  before preparation.
- Pi `--thinking high` is a scalar, not an inferred-benign option. The inferred
  benign matrix now uses bounded `--append-system-prompt` instead.
- Retained the controlled Pi effort positive: low maps to `minimal`; raw high is
  stripped from the executable projection with a redacted warning; the pure
  projection emits `--thinking minimal`; the original raw tuple remains intact.

## Verification

All guarded checks used the p6943 pre-import audit runner, a disposable home,
fake alias and bundle resolution where needed, and no real native/Mars/model
activity. The six public p6943 regressions passed. The focused owner cases from
`test_launch_resolution_runtime.py` passed (8 cases, including both selector
dry-run modes), and the Claude/Codex/Pi native-argument leaves passed (131
cases). Each guarded matrix reported **zero subprocess/network attempts**. The
unbuilt Pi extension was not needed by these focused cases.

`uv run ruff check .` passed. Canonical Pyright reported **0 errors** (100
existing warnings).

The entire runtime test module is not represented as a guarded pass: broad
unfiltered execution contains unrelated tests that try to launch Mars or probe
OpenCode. The focused R1 subset was run under the guard with explicit fakes.

## Remaining gates (not part of R1 P2)

Do not relax production grammar or treat this test correction as a G approval.
Preflight scalar reinjection and independent raw-vector acceptance remain
separate blocking downstream G/public gates and must be fixed and verified at
the actual emission boundary. No source production changes, native reads,
runner-history changes, or cost savings are claimed here.
