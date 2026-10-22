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

`build_permission_policy(rules, sandbox, approval, platform_rules, sandbox_write_roots)`
returns the frozen `PermissionPolicy`: `rules` (Mars's typed tool rules), `platform_rules`,
`sandbox`, the protected workspace roots and `approval`. Bind supplies control/task/context
roots separately from projected runtime and temp roots so a native read-only sandbox does
not block Meridian state. The roots are bind-time projection data and are excluded from
serialized policy dumps. The builder validates the two modes and never re-resolves
precedence; `launch/policies.py` already did. Each harness compiles the policy in
`harness/projections/permissions_<harness>.py`
and returns `(native value, EnforcementReport)`; User-facing behavior is in
`docs/agent-profiles.md#permissions-by-harness`.

**`ToolRule`** (`lib/tools.py`) is Mars's rule from the launch bundle's `tools.rules[]`:
`capability` + `pattern` carry meaning, `native` carries emission, and neither is derived
from the other. `native_status` is `known`, `absent` (the harness has no such tool) or
`unknown` (Mars has no name: a gap). A compile emits `native` as given and reads
`capability` only for mechanism it owns, such as Codex's switches and platform-rule
overrides. Meridian keeps no tool-name table. A scoped native is spelled `tool(scope)`;
`split_native` is its one reader, so compiles never parse it themselves. A bundle without `tools.rules`
fails with "install Mars >= 0.17.0"; the flat `allowed`/`disallowed` lists are not read.

**Legacy snapshots.** A launch snapshot an older build wrote holds `tools` (native key ->
action). `legacy_tool_rules` reads it as native-only rules (`capability=None`, origin
`legacy-snapshot`; the `*` key stays the `*` rule). A harness that can emit natives
(Claude, OpenCode) does; Codex's capability-keyed switches cannot interpret them. Report
lines for these rules carry `[legacy snapshot]`.

**Allowlists.** Two kinds. `is_allowlist` is the whole-policy kind: a `*: deny` rule, which
Mars adds for list-form `tools: [a, b]` and a map may write; `allowed_natives` names every
tool a rule leaves callable. The per-capability kind is a capability whose rules are scoped
allows and nothing unscoped: only those scopes may run, whatever `approval` says. A `*` rule
decides every unmatched call itself, so it ends that kind, as an unscoped rule for the
capability does. `build_permission_policy` writes each such allowlist into `rules` as the
unscoped deny it means (origin `allowlist`, natives the bases of the capability's scoped
natives, report element `tools: <capability> allowlist ...`), placed before Mars's rules.
So no compile has an allowlist path: each emits that deny as it emits a profile's plain deny
beside scoped allows. The one exception is Codex, whose shell switch would also refuse the
scoped allows and leave nothing to read files with, so its switches skip the origin. The
deny exists only on the bound policy; launch snapshots keep Mars's rules and the policy is
rebuilt at bind. Platform rules do not depend on the allowlist: `launch_platform_rules`
reads Mars's rules, and skips a platform deny only for an explicit deny of that capability
(`unscoped_action`), never for `*: deny`.

**Completion commands.** `launch_platform_rules` gives every spawn a `completion-commands`
platform rule: an allow, not a deny, for the shell commands a child runs to answer
Meridian's completion confirmation (`meridian spawn done` / `rearm`). It cannot depend on
the profile: a harness's own defaults can close the shell to a headless child (Claude asks
before any command that changes files). A child that cannot run them still closes when its
completion window runs out, but cannot reject a premature completion. `PlatformRule.commands`
names them bare; the compile of any harness that restricts the shell spells each command and
its argument form in its own scope syntax and puts them where a deny of `bash` or `*` does
not shadow them (OpenCode V1: `bash(<cmd>)` / `bash(<cmd> *)`; V2: the corresponding `shell`
rules). A compile whose shell is open has nothing to add. A harness that has not projected
it reports `unsupported`.

**Reports.** `complete_report` gives every policy element exactly one line, in policy order.
A compile reports what it enforces or has something specific to say about; the rest is
`unsupported` ("not projected for <Harness>"), a rule with `native_status: unknown` is a gap
("Mars has no <Harness> name"), and a deny of a tool the harness lacks (`absent`) is
satisfied.

**Codex switches.** Exact, unscoped `allow`/`deny` rules for `bash`, `web`, and `image`
become process-level shell, web-search, and image-viewing switches. The web-search grant
also accepts the exact `web_search` capability. Scoped rules and `ask` have no switch and
are reported unsupported. An unset sandbox is left unset, so Codex chooses its default.

## Circular Import Constraints

The dependency graph must remain:
- `budget.py` → `harness.common` (lazy only)

Do not add direct top-level imports between these modules and `harness.*` or `launch.*`.
The lazy import in `extract_cost_usd_from_json_line` is load-bearing.
