# cli/ — Startup Pipeline and Import Strategy

## Descriptor-Driven Startup

Every CLI invocation is classified against `COMMAND_CATALOG` before any
heavy module loads. `CommandDescriptor` carries all routing information:

- `startup_class` — determines which bootstrap call to make
- `state_requirement` — `none | project-read | runtime-read | project-write | runtime-write`
- `telemetry_mode` — `none | stderr | segment`
- `lazy_target` — import path string for the Cyclopts handler
- `root_source` — `"cwd"` (default) or `"argv"` (post-parse root resolution)
- `redirect` — optional redirect policy (e.g. `models list` → `mars models list`)

The catalog is the single source of truth for routing. The CLI startup path
never queries the extension registry for routing answers.

## Invocation Class Table

| Class | Examples | Bootstrap | Telemetry |
|---|---|---|---|
| `TRIVIAL` | `--help`, `--version` | none | none |
| `READ_PROJECT` | `config show`, `work current` | project-read | none |
| `READ_RUNTIME` | `spawn list`, `session log` | runtime-read | none |
| `WRITE_PROJECT` | `config init` | project-write | optional |
| `WRITE_RUNTIME` | `spawn create`, `work start` | runtime-write | segment |
| `PRIMARY_LAUNCH` | bare `meridian` | runtime-write | segment |
| `SERVICE_ROOTLESS` | `serve` | none | stderr |
| `SERVICE_RUNTIME` | `streaming serve` | runtime-write | segment |

Read-only classes install no telemetry, spawn no writer thread, create no
UUID, and make no filesystem mutations.

## Lazy Import Strategy

Four independent mechanisms reduce startup latency:

1. **Selective command registration** — `_register_commands_for_invocation()`
   imports only the module group matching the first positional token.
   Full registration fires only for `--help`.

2. **`lazy_dispatch.py`** — `make_lazy_command("module.path:function")` returns
   a callable that imports only when invoked. Cyclopts holds registrations
   without triggering imports.

3. **`app_tree.py` pattern** — defines top-level Cyclopts app objects
   (`spawn_app`, `session_app`, …) without importing any command implementations.
   Breaks the circular import where importing app objects pulled in all commands.

4. **Heavy module deferral** — `primary_launch` and `mars_passthrough` imported
   inside handler functions, not at module scope.

**Measured impact:** `main.py` module-import time 460ms → 156ms; root
`--help` latency 140ms → 54ms.

## Help Profile Selection

Human vs agent mode selects a `HelpProfile` at app-build time from the
command catalog — no runtime mutation of shared `App` objects. Do not call
`apply_agent_help_supplements()` (archived pattern).

## `require_established_project_root()`

`utils.py:require_established_project_root()` is the fail-fast guard used by
commands that require an actual project directory. It reads `GlobalOptions.project_root`
first (`-C` / `--directory`), then calls `resolve_project_root_resolution()`.
It raises `SystemExit(1)` when resolution falls back to bare CWD (`source="cwd"`)
without explicit or inherited project targeting — there is no marker walk-up.

The "established" in the name is deliberate: it makes the policy visible at
every call site. A command that calls this asserts it cannot proceed in an
arbitrary directory — it needs an explicit project root (`-C`, `MERIDIAN_PROJECT_DIR`,
or cwd that is intentionally the project root).

Do not use this in commands that should work anywhere (e.g. `config init`).
Use `resolve_project_root_resolution()` directly and handle the `source="cwd"`
case explicitly if the command has a sensible fallback.

**`SystemExit` is a `BaseException`, not `Exception`.** Code that wraps calls to
this function in `except Exception` will silently swallow the exit. Use
`except BaseException` (or let it propagate) in any wrapper that must not eat it.
`SystemExit` propagation matters whenever a guard is called from a context
that wraps in `except Exception` — the swallowed exit causes confusing crashes.

## `to_cli_output()` Dispatch

Command handlers emit results through `to_cli_output()` dispatch rather
than `isinstance` branches in `main.py`. This keeps the root CLI module
free of spawn-op and output-model imports. Each result type implements
the small protocol to produce its wire-shaped output.

## `init` Command Routing

`init_alias` in `main.py` has two branches:

- **With `--add` or `--link`** → routes through `init_ops.run_init_flow`, which
  handles package installation, config bootstrap, and tool directory linking in
  one call. Mars is invoked internally by `init_ops`, not via `mars_passthrough`.
- **Bare `meridian init`** (no flags) → calls `config_init_sync` directly to
  bootstrap the project config only.

There is no passthrough path in this command. `resolve_init_link_mars_command`
was deleted when `init_ops` took over the `--link` case — do not recreate it.
Auto-link is the default behavior; there are no confirmation prompts in the init
flow, so `--yes` has no meaning here and was removed.

## Session Initiation Argument Handling

`argv_normalization.py` is the central hub for `--fork`, `--fork-fresh`, `--from`,
and `--continue` argument handling. It runs before bootstrap and before Cyclopts
parsing.

### The Sentinel: `SELF_FORK_REF_SENTINEL = "__SELF__"`

Cyclopts requires a value for `str | None` parameters. Bare `--fork` (no value)
causes a parse error. The fix: `normalize_optional_value_flags(argv)` rewrites
bare and equals-form flags before Cyclopts sees them, inserting `__SELF__` as
the value token.

| Input form | Normalized form |
|---|---|
| `--fork` | `--fork __SELF__` |
| `--fork=` | `--fork __SELF__` |
| `--fork=p123` | `--fork p123` |
| `--from` | `--from __SELF__` |

`__SELF__` is not a valid spawn ref (`pN`), chat ref (`cN`), or UUID — no
collision risk. `SYNTHETIC_VALUE_TOKENS` (a `frozenset` containing `__SELF__`)
is imported by `bootstrap.py` so it can skip synthetic values during
passthrough-args splitting and positional-token detection.

`normalize_optional_value_flags()` is called in `main()` before bootstrap and
before Cyclopts parsing. The `mars` command is exempted (its args pass through
verbatim).

### Ref Resolution: `resolve_optional_ref(raw_ref, *, flag_name)`

When a handler receives `__SELF__`, it calls `resolve_optional_ref()` to resolve
it to the current session's spawn ID from `MERIDIAN_SPAWN_ID`. If
`MERIDIAN_SPAWN_ID` is not set (agent is not inside a managed session), it raises
`ValueError` with a flag-specific error message:

- `--fork` → `FORK_INFERENCE_ERROR`: "--fork preserves launch identity. Use --fork-fresh to change agent, model, or skills."
- `--from` → `FROM_INFERENCE_ERROR`

`resolve_fork_ref(raw_ref)` is a legacy wrapper for `--fork` callers; prefer
`resolve_optional_ref` with an explicit `flag_name`.

### Conflict Matrix: `validate_fork_mode()`

All mutual exclusion checks for session-initiation flags live in one place.
Both `spawn.py` (`_spawn_create`) and `primary_launch.py` (`run_primary_launch`)
call `validate_fork_mode()` and receive a `ForkModeResolution` — neither
re-implements validation.

Enforced conflicts:

| Combination | Error |
|---|---|
| `--fork` + `--fork-fresh` | Cannot combine |
| `--fork` + `--continue` | Cannot combine |
| `--fork-fresh` + `--continue` | Cannot combine |
| `--from` + `--continue` | Cannot combine |
| `--fork` + `--from` | Cannot combine (MVP limitation) |
| `--fork-fresh` + `--from` | Cannot combine (MVP limitation) |
| `--fork` + `--agent`/`--model`/`--skills` | Identity lock violation |

The identity lock (`--fork` blocks agent/model/skills overrides) is CLI policy,
not an ops invariant. The ops layer (`SpawnForkInput`) handles both
identity-preserving and identity-changing forks correctly. Non-CLI callers (MCP,
programmatic) may fork with identity changes legitimately. Do not push this check
into the ops layer.

### `ForkModeResolution` (frozen dataclass)

```
ForkModeResolution
  fork_ref: str | None          — resolved --fork ref (sentinel expanded)
  fork_fresh_ref: str | None    — resolved --fork-fresh ref
  is_fork: bool                 — True if either fork flag present
  is_fresh: bool                — True if --fork-fresh was used
  resolved_context_from: tuple[str, ...]  — resolved --from refs
```

### Pipeline Position

```
argv
  └── normalize_optional_value_flags()     ← rewrites bare/equals forms, inserts __SELF__
        └── _extract_global_options()      ← strips global flags, returns cleaned argv
              └── _split_passthrough_args() ← splits at --, skips SYNTHETIC_VALUE_TOKENS
                    └── Cyclopts parse      ← always sees a value for optional-value flags
                          └── validate_fork_mode()  ← conflict checks + ref resolution
```

## Related KB

→ [concepts/session-initiation.md](../../../../../../../.meridian/git/haowjy-meridian-cli-kb/kb/concepts/session-initiation.md) — four-mode session initiation semantics, identity lock, bare flag inference, and `--from` placement
→ [decisions/launch.md](../../../../../../../.meridian/git/haowjy-meridian-cli-kb/kb/decisions/launch.md) — rationale for the launch-mode split and argv normalization

## Spawn Output Mode Changes (spawn-return-report)

### Catalog default_output_mode for spawn

`("spawn",)` and `("spawn", "wait")` have `default_output_mode="text"` in `catalog.py`. This means agent mode (no explicit `--format`) uses compact text output, not JSON. All other spawn subcommands retain `"json"`.

Rule: if you add a new spawn subcommand, default to `"json"` unless it surfaces content agents need to read directly.

### `--metadata` flag

Added to `_spawn_create` and `_spawn_wait` handlers. In text mode, shows detailed inline accounting (model, harness, exit code, duration, cost, tokens, report path) while still including report body and transcript pointer.

Pattern: compact default → `--metadata` inline accounting → `spawn show` full record.

### Verbose threading at emit point

Both `_spawn_create` and `_spawn_wait` thread `--verbose` and `--metadata` into `FormatContext` at the emit point:

```python
if output_format != "json" and (metadata or verbose):
    emit(result.format_text(FormatContext(verbosity=1)))
else:
    emit(result)
```

This avoids touching the global emit pipeline. Use this same pattern if you add new spawn subcommands that need context-aware text formatting.

## Lateral Links

→ [../../lib/bootstrap/.context/CONTEXT.md](../../lib/bootstrap/.context/CONTEXT.md) — bootstrap functions invoked after classification
