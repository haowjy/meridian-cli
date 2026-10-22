# lib/safety/ — Context

Three focused subsystems consumed by the launch layer. None depends on another
except through lazy imports that break circular dependency cycles.

## Budget Enforcement (`budget.py`)

`LiveBudgetTracker` is a mutable streaming tracker. Feed it one raw stdout line at
a time via `observe_json_line(raw_line)`.

**Cost field priority:** extracts the first recognized field in order:
`total_cost_usd`, `cost_usd`, `cost`, `total_cost`, `totalCostUsd`.

**Monotonic update invariant:** `run_cost_usd` only increases. Harnesses emit
running totals, not deltas. If a new value is less than the current tracked value,
it is ignored. A harness that emits deltas instead of totals will under-count.

**Breach check:** returns `BudgetBreach` on first violation. Caller is responsible
for terminating the harness — the tracker does not stop it.

**Lazy import:** `extract_cost_usd_from_json_line` imports `harness.common` lazily
to break a circular dependency: `safety.budget` → `harness.common` → `harness.adapter`
→ `launch.launch_types` → `safety.policy`. Do not hoist this import to the module level.

## Post-Run Guardrails (`guardrails.py`)

`run_guardrails(guardrails, *, spawn_id, cwd, env, report_path, output_log_path)` runs
user-supplied scripts sequentially after spawn completion.

**All attempted regardless of earlier failures.** Returns `GuardrailResult(ok=False, failures=(...))`
with all collected failures.

**Env sanitization contract:** the child environment strips every key matching
`MERIDIAN_SECRET_*` before passing env to any guardrail script. This is a security
boundary — repo scripts are untrusted. Do not weaken this.

Injected env vars available to guardrail scripts:
- `_MERIDIAN_GUARDRAIL_RUN_ID` — the spawn ID
- `_MERIDIAN_GUARDRAIL_CHAT_ID` — chat ID for `meridian session log`, when known
- `_MERIDIAN_GUARDRAIL_REPORT` — optional path to `report.md`

**Command resolution:**
- Legacy native-Windows branch (untested): `.cmd`/`.bat` → `cmd.exe /d /c`;
  `.ps1` → `powershell.exe -NoProfile -NonInteractive -File`
- POSIX: executable bit → direct; else `bash <script>`; fallback on `OSError` → retry with `bash`

Exit code conventions: non-zero → `GuardrailFailure`; timeout → 124; `OSError` → 127.

## Permission Policy (`policy.py`)

`build_permission_policy(rules, sandbox, approval, platform_rules)` returns the frozen
`PermissionPolicy`: `rules` (Mars's typed tool rules), `platform_rules`, `sandbox` and
`approval`. It validates the two modes and never re-resolves precedence; `launch/policies.py`
already did. Each harness compiles the policy in `harness/projections/permissions_<harness>.py`
and returns `(native value, EnforcementReport)`; User-facing behavior is in
`docs/agent-profiles.md#permissions-by-harness`.

**`ToolRule`** (`lib/tools.py`) is Mars's rule from the launch bundle's `tools.rules[]`:
`capability` + `pattern` carry meaning, `native` carries emission, and neither is derived
from the other. `native_status` is `known`, `absent` (the harness has no such tool) or
`unknown` (Mars has no name: a gap). A compile emits `native` as given and reads
`capability` only for mechanism it owns: Codex's switches, the platform-rule overrides,
the sandbox inference. Meridian keeps no tool-name table. A bundle without `tools.rules`
fails with "install Mars >= 0.17.0"; the flat `allowed`/`disallowed` lists are not read.

**Legacy snapshots.** A launch snapshot an older build wrote holds `tools` (native key ->
action). `legacy_tool_rules` reads it as native-only rules (`capability=None`, origin
`legacy-snapshot`; the `*` key stays the `*` rule). A harness that can emit natives (Claude, OpenCode) does; Codex
honors a legacy `web_search` and infers no more than `read-only`. Report lines for these
rules carry `[legacy snapshot]`.

**Allowlists.** Two kinds, and `PermissionPolicy` answers for both so a compile asks and
does not re-derive: `is_allowlist` (a `*: deny` rule, which Mars adds for list-form
`tools: [a, b]` and a map may write), `allowlisted_capabilities` (capabilities whose only
rules are scoped allows: only those scopes may run, whatever `approval` says) and
`allowed_natives` (the harness names of every rule that leaves a tool callable).
A `*` rule decides every unmatched call itself, so it ends the per-capability kind; an
unscoped rule for the capability does the same. Platform rules do not depend on the allowlist:
`launch_platform_rules` skips a platform deny only for an explicit deny of that capability
(`unscoped_action`), never for `*: deny`. Each allowlisted capability is a report element
(`allowlist_element`), so a harness that does not enforce it says `unsupported` by default.

**Completion commands.** `launch_platform_rules` gives a spawn that carries tool rules a
`completion-commands` platform rule: an allow, not a deny, for the shell commands a child
runs to answer Meridian's completion confirmation (`meridian spawn done` / `rearm`). A
child that cannot run them still closes when its completion window runs out, but cannot reject
a premature completion. `PlatformRule.commands` names them bare; the compile of any harness that
restricts the shell spells each command and its argument form in its own scope syntax and puts
them where a deny of `bash` or `*` does not shadow them (OpenCode: `bash(<cmd>)` and
`bash(<cmd> *)`, which V2 orders after the broad rules). A spawn without tool rules has an
open shell and gets no rule. A harness that has not projected it reports `unsupported`.

**Reports.** `complete_report` gives every policy element exactly one line, in policy order.
A compile reports what it enforces or has something specific to say about; the rest is
`unsupported` ("not projected for <Harness>"), a rule with `native_status: unknown` is a gap
("Mars has no <Harness> name"), and a deny of a tool the harness lacks (`absent`) is
satisfied.

**Sandbox inference** (Codex only, `permissions_codex.py`): with `sandbox: default` the
Codex compile infers one from the `bash`, `edit`, `external_directory` and `*` capabilities.
No other harness infers a sandbox. It is deleted with the Codex commit.

## Circular Import Constraints

The dependency graph must remain:
- `budget.py` → `harness.common` (lazy only)

Do not add direct top-level imports between these modules and `harness.*` or `launch.*`.
The lazy import in `extract_cost_usd_from_json_line` is load-bearing.
