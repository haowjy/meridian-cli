# lib/safety/

Three independent enforcement subsystems the launch layer assembles; none drives the spawn:
budget tracking (`budget.py`), post-run guardrails (`guardrails.py`), and the
transport-neutral permission policy (`policy.py`) each harness compiles into its own flags.

- **Budget updates are monotonic.** `LiveBudgetTracker` tracks running totals; a lower value
  is ignored. A harness emitting deltas will under-count and breach detection fails silently.
- **The tracker detects a breach but does not stop the spawn**: the caller must stop the
  harness on `BudgetBreach`.
- **Guardrails all run, even after a failure**; failures collect into `GuardrailResult`.
- **Guardrail env sanitization is a security boundary**: every `MERIDIAN_SECRET_*` key is
  stripped before any guardrail script runs, because repo scripts are untrusted.
- **Don't hoist the lazy import in `budget.extract_cost_usd_from_json_line`**:
  `safety.budget` → `harness.common` → `harness.adapter` → `launch.launch_types` →
  `safety.policy` is a load-time cycle.
- **`PermissionPolicy.network` is always resolved** (`Allow(deny)` or `Hosts(allow, deny)`,
  empty Hosts meaning deny); `SandboxDerivedNetwork` exists only on the bundle result until
  final sandbox precedence is applied. Nothing in `safety/` interprets harness-owned
  user-config diagnostics.

[.context/CONTEXT.md](.context/CONTEXT.md) — cost field ordering, guardrail command
resolution, permission policy, import map. Bind-time platform rules:
`../launch/permissions.py`.
