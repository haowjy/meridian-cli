# tool_policy_runtime/ — Context

`evaluate.py` is the only place a tool call is decided against a launch's rules. Pi's
`meridian-permissions` extension (`pi_runtime/`) and Codex's `PreToolUse` hook each run it
once per call and act on the answer; neither holds rule-matching code. The request and
answer shapes are in the module docstring. The policy is `ExtensionPolicy`
(`lib/harness/projections/tool_extension_policy.py`), JSON in `_MERIDIAN_TOOL_POLICY`.

- **Stdlib only, no Meridian imports.** Codex starts it per shell call; a CLI import costs
  too much (#721). The interpreter is the one Meridian ran on, passed explicitly with `-I`
  (`evaluator_argv()`; Pi gets it as JSON in `_MERIDIAN_TOOL_EVALUATOR`).
- **Fail closed.** A missing policy, bad request, crash, timeout or unreadable answer refuses
  on both callers. Codex's hook command wraps any non-2 failure into a refusal; the Pi
  extension blocks on any failure to get a valid answer.
- **Command grants before rules.** Meridian's own commands (`meridian_commands`) are allowed
  as a whole command before any rule is read.
- **Ask.** The evaluator answers `ask`; Pi prompts in a primary TUI and rejects otherwise with
  the policy's `headless_reject_reason`. Codex cannot ask, so its compile turns `ask` into
  `deny` and the script refuses any `ask` it still sees.
- **Decision table:** `tests/fixtures/tool_policy_cases.json`, run in
  `tests/unit/harness/test_tool_policy_evaluator.py`. Change a decision there first.
- `bash_fail_closed` and which tools need the evaluator at all are compile-time choices in
  `tool_extension_policy.py` (`bash_fails_closed`, `needs_extension`).

Do not add a shell parser to close individual gaps: wrappers, `bash -c`, substitutions and
interpreters get past the splitter by design, and it is an accident guard, not a sandbox.
