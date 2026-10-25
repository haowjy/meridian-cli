# cli/ — CLI Entry Point and Startup Pipeline

Command-line surface. Descriptor-driven: every invocation is classified before any heavy module imports. Fast paths stay fast; heavy operations load only when needed.

## Mental Model

```
argv
  └── canonicalize_argv()  ← pre-classifier: optional refs + bare --continue routing
        └── classify_invocation()       ← longest-prefix match against COMMAND_CATALOG
                    └── CommandDescriptor     ← routing information for this invocation
                    ├── startup_class   → telemetry mode + primary-launch handling
                    ├── bootstrap_plan  → state preparation requirement
                    ├── telemetry_mode
                    └── root_source     → cwd or post-parse argv root resolution
```

`COMMAND_CATALOG` in `startup/catalog.py` is the single source of truth for routing. The startup path never queries the extension registry or any command module for routing decisions. Classification happens before anything expensive loads.

## Key Rules

- **Handler imports stay local.** Every group registers through
  `register_extension_cli_group`, which reads the first-party registry's unresolved
  declarations; only an auto-generated handler resolves its operation. Resolving
  the whole registry (`list_all`, `list_for_surface`) imports every operation.
- **Selective command imports.** `_register_commands_for_invocation()` imports and explicitly registers only the module group matching the first positional token. Full registration fires only for `--help`.
- **`app_tree.py` breaks the circular import.** It defines top-level Cyclopts app objects (`spawn_app`, `session_app`, etc.) without importing any command implementations. If you import command implementations from `app_tree.py`, selective registration collapses into importing everything.
- **`invocation.py` owns invocation state.** Command modules import `emit`, `get_global_options()`, and `current_output_sink()` from there. They never import `main.py` or receive those values through registration callbacks.
- **Every command runs inside `invocation_scope()`.** `main.py` installs parsed options around bootstrap and dispatch separately, and `main()` holds one warning cache across both so a command warns once per key; tests and other in-process callers must install their own explicit `GlobalOptions`. `get_global_options()` raises outside that boundary rather than inventing defaults.
- **`to_cli_output()` dispatch, not `isinstance` in `main.py`.** Command handlers return typed result objects. Each implements `to_cli_output()`. Adding result types does not require editing `main.py`.
- **Read-only invocation classes never initialize authority or telemetry.** `READ_ROOTLESS`, `READ_PROJECT`, and `READ_RUNTIME` never allocate project/session IDs or create missing runtime roots. Discovery within an existing runtime may catch up its disposable history index under the state layer's coordination locks; it must not mutate lifecycle authority.
- **`-C` has no observer identity.** `resolve_cli_observer_context()` supplies the
  ambient spawn ID only when observation is enabled and the invocation did not use
  `-C`. Commands directed at another project may read results but never consume them;
  `spawn ack -C` is refused because acknowledgement has no read-only meaning.
- **Every real subcommand gets an exact descriptor.** `classify_invocation()` falls back to the longest matching prefix, so a missing `spawn <sub>` descriptor silently inherits `spawn`'s `WRITE_RUNTIME` bootstrap and telemetry class. Add the descriptor with the command; delete descriptors whose command no longer exists. No test catches drift.
- **Project identity has committed and ad-hoc tiers.** Every cwd is eligible.
  Write preparation mints a marked project's ID or materializes path-derived state
  under the user home; it never creates `meridian.toml` in a bare directory.
  Read-only preparation creates neither identity nor state.
- **`root_source = RootSource.ARGV`** commands (e.g. `meridian init [path]`) defer bootstrap until after argument parsing. They call `prepare_for_project_write(arg_derived_root)` themselves — they don't receive a pre-prepared context.
- **Init is the tier boundary.** `init` / `config init` atomically write the
  scaffold and committed ID before bootstrap. They never move ad-hoc history;
  output names a previously materialized `adhoc.*` root instead.
- **Doctor is a pure observer when no runtime root exists.** Its project repairs
  are gated on an existing root; `--global` may scan and explicitly prune state
  but must not mint identity or initialize the user Mars root.
- **`validate_fork_mode()` is the single place for fork/from/continue conflict checks.** `spawn.py` and `primary_launch.py` both call it and consume `ForkModeResolution`. Do not re-implement conflict logic or ref resolution in handlers.
- **`canonicalize_argv()` runs before every classifier.** It normalizes bare `--fork`/`--fork-fresh`/`--from` values and routes bare `--continue` to `session browse`. Never classify or call Cyclopts on raw argv containing these flags.
- **Session browse keeps process transfer outside the TUI.** `session_cmd.py` selects plain, interactive, or refused presentation before loading `cli/browse/`. The picker returns a typed re-entry decision; only the handler may `exec` it after the full-screen application has closed.
- **Register nested handlers by call, not decorator.** Inside a `register_*_commands()` function, define the handler, then call `sub_app.command(fn, name=...)` / `sub_app.default(fn)` (same for prompt_toolkit `bindings.add(key)(fn)`). Pyright's `reportUnusedFunction` flags a local function whose only use is a decorator, whatever its name. Module-level decorated commands (`kg_cmd.py`, `qi_cmd.py`) are fine.
- **Cyclopts private API goes through `cyclopts_internals.py`.** Help rendering needs `App._assemble_help_panels` and `Group._show`. Only that module touches them, and it carries the per-line suppressions. Recheck it when upgrading Cyclopts.
- **Positional-prompt subcommand guard.** `_validate_positional_spawn_prompt()` in `spawn.py` rejects bare positional prompts that look like a subcommand (`^[a-z][a-z-]*$` single token) but are not in the registered subcommand set. The subcommand set is derived from the commands registered with Cyclopts, not hardcoded. `-p`/`--prompt`/`--prompt-file` bypass this guard and accept any string. The did-you-mean hint uses Damerau-Levenshtein (adjacent-transposition-aware) distance.

## Invocation Classes

| Class | Examples | State prep (`state_requirement`) |
|---|---|---|
| `TRIVIAL` | `--help`, `--version` | none |
| `READ_ROOTLESS` | `doctor`, `qi`, `kg check`, `config show`, `skills` | none |
| `READ_PROJECT` | `context`, `hooks list` | `prepare_for_project_read()` |
| `READ_RUNTIME` | `spawn list`, `session log` | `prepare_for_runtime_read()` |
| `WRITE_PROJECT` | `config init` | `prepare_for_project_write()` |
| `WRITE_RUNTIME` | `spawn`, `work start` | `prepare_for_runtime_write()` |
| `PRIMARY_LAUNCH` | bare `meridian` | `prepare_for_runtime_write()` |
| `SERVICE_ROOTLESS` | `serve` | none |
| `SERVICE_RUNTIME` | `streaming serve` | `prepare_for_runtime_write()` |

`startup_class` drives telemetry installation and primary-launch background repairs — not bootstrap. Bootstrap is selected solely by `state_requirement`.

The default `spawn` route is the deliberate exception within `WRITE_RUNTIME`: its
descriptor performs project-read bootstrap only. The ops preparation path must resolve
and enforce an inherited caller boundary before it materializes runtime state. Every
launch uses that same deferred materialization path. Other mutating spawn
subcommands retain normal runtime-write bootstrap.

## Entry Points

- `entrypoint.py` — `main()`: trivial fast paths (help/version), delegates to `main.py`
- `main.py` — full dispatch via `_register_commands_for_invocation()`
- `invocation.py` — typed per-invocation options and output routing
- `app_tree.py` — Cyclopts app objects without command implementations
- `session_cmd.py` / `browse/` — presentation gate and full-screen session picker
- `argv_normalization.py` — pre-classifier canonicalization of session-initiation flags; `validate_fork_mode()`; `ForkModeResolution`
- `startup/catalog.py` — `COMMAND_CATALOG`, `CommandDescriptor`
- `startup/classify.py` — `classify_invocation()`
- `skills_cmd.py` — rootless read surface for the packaged `meridian.skills` data

## Anti-Patterns

- Don't add heavy imports at module scope in command modules — they load on first invocation, not at startup.
- Don't add routing logic in `main.py` outside of the catalog — the descriptor carries all routing decisions.
- Don't import `main.py` from a command module or inject invocation accessors through `register_*`; use `invocation.py`.
- Don't mutate shared `App` objects for help profile selection — `HelpProfile` is selected at build time per invocation.
- Don't re-implement fork/continue/from conflict checks in handlers — call `validate_fork_mode()` and use `ForkModeResolution`.
- Don't push the identity lock (`--fork` + model/agent/skills conflict) into the ops layer — it's CLI ergonomic policy, not an ops invariant.
- Don't hardcode subcommand names in the positional-prompt guard — derive the set from registered commands so new subcommands are automatically covered.

## Related

- `../lib/bootstrap/` — bootstrap functions called after classification
- `../lib/core/resolved_context.py` — `ResolvedContext` for primary launch

→ [.context/CONTEXT.md](.context/CONTEXT.md) — invocation class table, lazy import strategy, session initiation argument handling, app_tree circular-import pattern, help profile selection
→ KB: `$MERIDIAN_CONTEXT_KB_DIR/architecture/startup-pipeline.md` (see `meridian context kb`)
