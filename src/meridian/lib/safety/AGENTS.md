# lib/safety/

Four enforcement subsystems consumed by the launch layer: cost budget tracking,
post-run guardrails, tool permission resolution, and secret redaction. Each is
independent — no module imports another except through carefully managed lazy imports
that prevent circular dependencies.

## Mental Model

All four subsystems enforce constraints on what a spawn can do or expose:

- **Budget** — streaming cost enforcement; terminates if spend exceeds limit
- **Guardrails** — post-run scripts that verify or audit after spawn completes
- **Permissions** — translates sandbox/approval intent into harness-specific flags
- **Redaction** — strips credentials from output before it reaches logs or users

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

**Secret redaction uses longest-first ordering.** If a shorter secret is a substring
of a longer one, longest-first prevents the shorter secret from partially redacting
the longer secret's placeholder, producing garbage output.

**Do not hoist the lazy import in `budget.py`.** `extract_cost_usd_from_json_line`
imports `harness.common` lazily to break a circular dependency: `safety.budget` →
`harness.common` → `harness.adapter` → `safety.permissions`. Moving it to module
level creates a circular import at load time.

## Permission Resolver Selection

`build_permission_resolver()` selects a resolver based on what's configured:

| Condition | Resolver |
|---|---|
| Neither list provided | `TieredPermissionResolver` — harness derives from sandbox/approval |
| `tools` provided | `ToolsPermissionResolver` (abstract ToolsField compiled per harness) |
| Explicit unsafe opt-out | `UnsafeNoOpPermissionResolver` + warning |

Tools compilation preserves scoped patterns where the target harness supports them
(notably OpenCode) and projects Claude capability aliases to tool names.

## Entry Points

- `budget.py` — `LiveBudgetTracker`, `Budget`, `BudgetBreach`
- `guardrails.py` — `run_guardrails()`
- `permissions.py` — `PermissionConfig`, `build_permission_resolver()`
- `redaction.py` — `redact_secrets()`, `redact_secret_bytes()`

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — budget cost field ordering, guardrail
command resolution by platform, permission resolver table, redaction edge cases,
circular import dependency map

## Related

- `../launch/permissions.py` — owns the actual permission pipeline resolution
- `../harness/common.py` — provides `iter_nested_dicts` used by budget extraction
