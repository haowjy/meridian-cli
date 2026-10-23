# lib/safety/

Three enforcement subsystems consumed by the launch layer: cost budget tracking,
post-run guardrails, and tool permission resolution. Each is
independent — no module imports another except through carefully managed lazy imports
that prevent circular dependencies.

## Mental Model

All three subsystems enforce constraints on what a spawn can do:

- **Budget** — streaming cost enforcement; terminates if spend exceeds limit
- **Guardrails** — post-run scripts that verify or audit after spawn completes
- **Permissions** — the transport-neutral policy each harness compiles into its own flags

The launch layer assembles these; none of them drives the spawn itself.

## Key Rules

**Budget updates are monotonic.** `LiveBudgetTracker` tracks running totals, not
deltas. If a harness emits a value lower than the current tracked total, it's ignored.
A harness that emits deltas instead of totals will under-count — breach detection
will fail silently.

**The tracker detects a breach but does not terminate the spawn.** The caller is
responsible for stopping the harness on `BudgetBreach`. If you forget this, the
spawn runs past the budget limit without stopping.

**Guardrails always run regardless of earlier failures.** All scripts execute
sequentially even if one fails; all failures are collected into `GuardrailResult`.

**Guardrail env sanitization is a security boundary.** The child environment strips
every key matching `MERIDIAN_SECRET_*` before passing env to any guardrail script.
Repo scripts are untrusted. Do not weaken this.

**Do not hoist the lazy import in `budget.py`.** `extract_cost_usd_from_json_line`
imports `harness.common` lazily to break a circular dependency: `safety.budget` →
`harness.common` → `harness.adapter` → `launch.launch_types` → `safety.policy`. Moving it to module
level creates a circular import at load time.

## Permission Policy

`policy.py` builds one `PermissionPolicy` per launch: the tool and platform rules, the
sandbox mode, the optional authored network mode (`network_mode` supplies its `allow`
default), the
explicit workspace roots a native read-only sandbox must protect, and the approval mode,
already resolved. `ResolvedLaunchSpec.permission_policy` carries it, and
each harness compiles it in `harness/projections/permissions_<harness>.py`. Nothing in
`safety/` knows a harness. The tool rules are Mars's typed rules (`lib/tools.py`). The policy
answers the allowlist questions (`is_allowlist`, `allowed_tools`) and writes each
per-capability allowlist into its rules as an unscoped deny, so no compile re-derives them.
`EnforcementReport` (a tuple of `ReportLine`) is the shape each compile reports what it
enforced in. Harness projection ports separately expose the pure effective-network function
shared by that line and the caller guardrail. The formatting helpers live here too.

## Entry Points

- `budget.py` — `LiveBudgetTracker`, `Budget`, `BudgetBreach`
- `guardrails.py` — `run_guardrails()`
- `policy.py` — `PermissionPolicy`, `build_permission_policy()`, `complete_report()`

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — budget cost field ordering, guardrail
command resolution by platform, permission policy,
circular import dependency map

## Related

- `../launch/permissions.py` — bind-time platform rules (nested-agent and built-in-agent
  denies, exact/prefix Meridian command grants)
- `../harness/common.py` — provides `iter_nested_dicts` used by budget extraction
