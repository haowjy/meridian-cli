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
`sandbox`, the protected workspace roots and `approval`. Spawn capability is derived from
the platform command marked as running outside the sandbox. Bind supplies control/task/context
roots separately from projected runtime and temp roots so a native read-only sandbox does
not block Meridian state. The roots are bind-time projection data and are excluded from
serialized policy dumps. The builder validates the two modes and never re-resolves
precedence; `launch/policies.py` already did. Each harness compiles the policy in
`harness/projections/permissions_<harness>.py`
and returns `(native value, EnforcementReport)`; User-facing behavior is in
`docs/agent-profiles.md#permissions-by-harness`.

**`ToolRule`** (`lib/tools.py`) is Mars's rule from the launch bundle's `tools.rules[]`:
`capability` + `pattern` carry meaning, and Mars gives two native facts that nothing derives
from each other or from the capability. `native` is the permission-rule spelling, already
scoped; a compile emits it as given. `native_tools` (read through `ToolRule.tools`) is the
unscoped harness tools the rule concerns; every question of which tools a rule leaves
callable or removes reads it: Claude and Pi `--tools`, Pi `--exclude-tools`, Claude's
gating, `PermissionPolicy.allowed_tools` and the allowlist deny below. The two differ where
a harness scopes a tool through another's permission: Claude checks paths only through
`Edit(path)` and `Read(path)`, so `write(src/*)` is `Edit(src/*)` but concerns `Write`, and
`grep(src/*)` is `Read(src/*)` but concerns `Grep`. `native_status` is `known`, `absent`
(the harness has no such tool) or `unknown` (Mars has no name: a gap). A compile reads
`capability` only for mechanism it owns, such as Codex's switches and platform-rule
overrides. Meridian keeps no tool-name table. A scoped native is spelled `tool(scope)`;
`split_native` is its one reader, so compiles never parse it themselves. A bundle without
`tools.rules`, or whose rules lack `native_tools`, fails with "install Mars >= 0.17.0"; the
flat `allowed`/`disallowed` lists are not read. `tools.capability_natives` (the platform
denies) carries no `native_tools`: those names are unscoped, so the reader gives them as
the tools too.

**Legacy snapshots.** A launch snapshot an older build wrote holds `tools` (native key ->
action). `legacy_tool_rules` reads it as native-only rules (`capability=None`, origin
`legacy-snapshot`; the `*` key stays the `*` rule). A harness that can emit natives
(Claude, OpenCode) does; Codex's capability-keyed switches cannot interpret them. Report
lines for these rules carry `[legacy snapshot]`. Rules from such a snapshot, and
`tool_rules` persisted before Mars gave `native_tools`, have `native_tools=None`;
`ToolRule.tools` then reads each native name unscoped. That is right wherever the spelling
names its own tool and wrong for Claude's category spellings (`Edit(src/*)` for a scoped
write), so Claude reports a scoped allow or ask read that way as `partial`.

**Allowlists.** Two kinds. `is_allowlist` is the whole-policy kind: a `*: deny` rule a map
writes; `allowed_tools` names every
tool a rule leaves callable (from `native_tools`, unscoped). The per-capability kind is a capability whose rules are scoped
allows and nothing unscoped: only those scopes may run, whatever `approval` says. A `*` rule
decides every unmatched call itself, so it ends that kind, as an unscoped rule for the
capability does. `build_permission_policy` writes each such allowlist into `rules` as the
unscoped deny it means (origin `allowlist`; its natives and tools are the `native_tools`
of the capability's scoped rules, since an unscoped rule's spelling is its tool; report
element `tools: <capability> allowlist ...`), placed before Mars's rules.
Mars resolved overlap among the rules it wrote (for natives two unscoped rules share, the
capability whose natives are a strict subset wins when the actions differ) but never saw
this deny, so `build_permission_policy` resolves it the same way against the other unscoped
rules: `edit: allow` beside `write(src/*): allow` loses `Write`, and `edit(src/*): allow`
beside `write: allow` leaves `Write` out of the deny. Otherwise each compile would read
the broad allow its own way (Claude ran every write). So no compile has an allowlist path: each emits that deny as it emits a profile's plain deny
beside scoped allows. The one exception is Codex, whose shell switch would also refuse the
scoped allows and leave nothing to read files with, so its switches skip the origin. The
deny exists only on the bound policy; launch snapshots keep Mars's rules and the policy is
rebuilt at bind. Platform rules do not depend on the allowlist: `launch_permission_rules`
denies `agent` on every launch without native agent copies, whatever the profile says, and
skips a spawn's `task` deny only for the profile's explicit unscoped `task: allow|ask` or an
existing unscoped deny of it (`unscoped_action`), never for a `*` rule.

**Meridian commands.** `launch_permission_rules` gives every launch one
`meridian-commands` platform rule. Completion (`meridian spawn done` / `rearm`) and audited
read commands are always present; a profile with spawn capability also receives the
`meridian spawn` prefix. Each `PlatformCommand` declares its role and `exact` or `prefix`
match. Completion is not profile-restrictable; read and launch roles are, so a narrower or
equal scoped profile deny/ask remains stricter for those commands. A harness that restricts
the shell spells each match in its native syntax, reports `partial` when it cannot honor the
role's precedence, and reports `unsupported` when it has no carrier.

A spawn-capable read-only Codex child normally writes completion signals through Meridian's
user-wide execpolicy rule. Without the spawn grant, or if that rules-file write failed, the
connection declares no writable completion-signal channel: its hold skips confirmation and
reads no `done` or `rearm` signal while retaining the same evidence, blocker, and deadline
policy.

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
