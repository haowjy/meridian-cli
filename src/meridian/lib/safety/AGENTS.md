# Safety

Budget observations are cumulative: feeding deltas to the monotonic tracker
under-counts. A breach is only a decision; its caller stops the harness.

Guardrail scripts are untrusted repository code. Strip inherited
`MERIDIAN_SECRET_*` values before running them.

The permission policy itself is a core type: `lib/core/permission_policy.py`,
[../core/.context/permission-policy.md](../core/.context/permission-policy.md).
