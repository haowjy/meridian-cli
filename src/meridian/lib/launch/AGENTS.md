# launch — composition and execution

This layer connects spawn policy to harness mechanisms. Prepare the launch once and
bind it for each preview or execution; adapters consume the bound argv, environment
and permissions rather than composing their own. Preparation can call Mars; binding
may run a bounded direnv subprocess even for previews. Materialization owns writes
for a real launch.

- Resolve launch inputs through `resolve_launch_inputs()` before persisting a row.
  Materialize a fork only after the row exists: its session artifact refers to the row.
- Keep `SpawnRequest.prompt` raw: background execution and session changes prepare
  again, and a composed prompt would duplicate references and prior context.
- Put composition warnings in `LaunchContext.warnings`, where callers can see them.
- Execute one attempt per turn; do not replay a model turn after a terminal or
  transport failure. Late diagnostics must use the published-spawn artifact mutation
  seam so they cannot recreate a directory deleted by retention.
- Spawn execution uses `LaunchArgvIntent.SPEC_ONLY`; primary launch also uses
  `REQUIRED` to construct its command. Preserve JSON-safe request fields for the
  background worker's disk round trip.
- Use `session_scope()` for managed backend cleanup; do not add competing cleanup
  after its exit.

[Context](.context/CONTEXT.md) covers continuation, prompt caching, directories,
workspace projection and skills. [Process](process/AGENTS.md) covers primary execution;
[state](../state/AGENTS.md) covers launch locks. Related layers: [spawn policy](../ops/spawn/AGENTS.md)
and [harness mechanisms](../harness/AGENTS.md).
