Final verification for active-work context resolution after fix p5005.

Please verify:
- `meridian context work` active dir semantics still pass.
- env-first then persisted-current fallback still passes.
- work start/switch persistence still passes.
- work done and update status=done clear persisted current work if the item is current.
- spawn work resolution tests still pass.

Run focused lint/tests/pyright as appropriate. Do not edit unless purely mechanical and necessary; report changed files if any.
