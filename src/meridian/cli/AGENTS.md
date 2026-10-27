# cli/ — CLI Entry Point and Startup Pipeline

Descriptor-driven: every invocation is classified before any heavy module imports,
so fast paths stay fast. Pipeline, invocation-class table, lazy-import strategy, and
session-initiation handling: `.context/CONTEXT.md`.

`canonicalize_argv()` → `classify_invocation()` (longest-prefix match against
`COMMAND_CATALOG` in `startup/catalog.py`) → `CommandDescriptor` (`startup_class`,
`bootstrap_plan`, `telemetry_mode`, `root_source`).

## Rules

- **`COMMAND_CATALOG` is the only routing source.** The startup path never queries the
  extension registry or a command module, and `main.py` carries no routing logic.
- **Every real subcommand gets an exact descriptor.** The longest-prefix fallback means
  a missing `spawn <sub>` descriptor silently inherits `spawn`'s `WRITE_RUNTIME`
  bootstrap and telemetry class. Add it with the command; delete it with the command.
- **`startup_class` drives telemetry and primary-launch repairs; bootstrap comes only
  from `state_requirement`.** The default `spawn` route is the exception inside
  `WRITE_RUNTIME`: project-read bootstrap only, because ops must resolve the inherited
  caller boundary before materializing runtime state. Other mutating `spawn`
  subcommands keep runtime-write bootstrap.
- **Read-only classes never initialize authority or telemetry.** `READ_ROOTLESS`,
  `READ_PROJECT`, `READ_RUNTIME` never mint project/session IDs or create runtime roots;
  they may only catch up the disposable history index, never lifecycle authority.
- **Project identity has committed and ad-hoc tiers.** Write preparation mints a marked
  project's ID or materializes path-derived state under the user home; it never creates
  `meridian.toml` in a bare directory. `init` / `config init` write the scaffold and
  committed ID atomically before bootstrap and never move ad-hoc history.
- **`root_source = RootSource.ARGV`** commands (`meridian init [path]`) defer bootstrap
  and call `prepare_for_project_write(arg_derived_root)` themselves.
- **`doctor` is a pure observer when no runtime root exists.** `--global` may scan and
  prune state but must not mint identity or initialize the user Mars root.
- **Handler imports stay local.** Groups register through `register_extension_cli_group`,
  which reads unresolved declarations; resolving the whole registry (`list_all`,
  `list_for_surface`) imports every operation. `_register_commands_for_invocation()`
  imports only the group matching the first positional token (all groups only for `--help`).
- **`app_tree.py` must not import command implementations**, or selective registration
  collapses into importing everything.
- **`invocation.py` owns invocation state.** Commands import `emit`,
  `get_global_options()`, `current_output_sink()` from there, never from `main.py`
  or through `register_*` callbacks. Everything runs inside `invocation_scope()`
  (`main()` shares one warning cache across bootstrap and dispatch); in-process
  callers and tests install explicit `GlobalOptions`.
- **Handlers return typed results implementing `to_cli_output()`**; no `isinstance`
  dispatch in `main.py`.
- **`-C` drops the caller's identity.** `main._directory_env_scope` unsets the spawn,
  chat, parent, and work IDs for the whole invocation (they name another project's
  records), so context, ops, telemetry, and child env are unattributed. Depth stays: it
  is the recursion guard. Commands aimed at another project may read results, never
  consume them. `spawn ack -C` is refused.
- **Fork/from/continue conflicts live in `validate_fork_mode()`** (`argv_normalization.py`),
  consumed by `spawn.py` and `primary_launch.py` via `ForkModeResolution`. The
  `--fork` + model/agent/skills identity lock is CLI policy, not an ops invariant.
- **`canonicalize_argv()` runs before every classifier.** Never classify or call Cyclopts
  on raw argv containing `--fork`, `--fork-fresh`, `--from`, or bare `--continue`.
- **Session browse keeps process transfer outside the TUI.** The picker returns a typed
  re-entry decision; only the `session_cmd.py` handler may `exec` it, after the
  full-screen app has closed.
- **Register nested handlers by call**, not decorator (`sub_app.command(fn, name=...)`,
  `bindings.add(key)(fn)`): pyright's `reportUnusedFunction` flags local functions
  used only as decorators. Module-level decorated commands are fine.
- **Cyclopts private API goes through `cyclopts_internals.py`** (carries the per-line
  suppressions); recheck it when upgrading Cyclopts.
- **Don't mutate shared `App` objects for help profiles**; `HelpProfile` is selected at
  build time per invocation.
