# lib/safety/

Four focused subsystems consumed by the launch layer. No module depends on another
except through lazy imports that break circular dependency cycles.

## Entry Points

- `budget.py` — `LiveBudgetTracker`, `Budget`, `BudgetBreach`: streaming cost enforcement
- `guardrails.py` — `run_guardrails()`: post-run script execution
- `permissions.py` — `PermissionConfig`, `build_permission_resolver()`: tool access control
- `redaction.py` — `redact_secrets()`, `redact_secret_bytes()`: credential scrubbing

## Depth

See [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Budget cost field ordering and monotonic update invariant
- Guardrail env sanitization contract (MERIDIAN_SECRET_* stripping)
- Permission resolver selection logic and tool name normalization
- Secret redaction ordering (longest-first)
- Circular import constraints

## Related

- `../launch/permissions.py` — owns the actual permission pipeline resolution
- `../harness/common.py` — provides `iter_nested_dicts` used by budget extraction
