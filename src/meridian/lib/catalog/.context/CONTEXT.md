# Catalog context

## Mars roots

Mars chooses the root; Meridian never does. Every Mars call, launch bundle and passthrough
included, gets its place from `mars_location`: it runs in the caller's working directory, so a
relative path the caller typed (`meridian mars add ./x`) means what they meant, and a project
Meridian was told about (an explicit path or `MERIDIAN_PROJECT_DIR`) that the working directory
is not inside goes as `--root`. For Mars `--root` is only where its walk-up starts: Mars picks
the nearest `mars.toml`, else the user level for a lookup, and resolves a relative path from the
working directory when it exists there. Home is never a project, so a project named as exactly
home names none and gets no `--root` (Mars would otherwise open a leftover `~/mars.toml`, and
refuses `--root ~` from its next minor). Meridian does no walk-up of its own. A write with no project fails in Mars with `project_not_found` (`is_project_not_found`;
the passthrough's `add`/`link` gate keys on it). Where Meridian must know the root
(`config show`, `context`, `doctor`, the bootstrap store), it reads the `root` of Mars's
`--json` output (`MarsRoot`, `parse_mars_root`), from `mars agents list`. A launch lists
agents once (`try_mars_agent_inventory`, passed to `compile_prepared_policy_surface` and
`prepare_launch_surface`), for the bootstrap store, for whether the harness has native agent
copies, and for the store a replayed relative profile path resolves in. When Mars can't answer,
`MarsRootReport.error` carries why (no binary, or Mars's own message) and `doctor` and
`config show` warn with it.
Meridian reads installed files only where Mars points (an agent's `source_path`, the store
`root` names: a project's store is its documented `.mars/`, the user level is its own store at
`root.path`), and never Mars's state files (`native-agents.json`, `models-merged.json`, locks):
ask Mars instead. Where the user level lives (`MARS_HOME`, else `~/.mars`) is Mars's rule;
Meridian never computes it. Launch bootstrap docs are read from
the store `root` names (`BootstrapRegistry`), since Mars reports no path for them; `meridian
init` reports content from `mars list --json` and runs `mars init`, a no-op on an existing
project, instead of checking for `mars.toml` itself.

`~/.meridian/mars` (`LegacyUserRoot`) is the one exception for one minor release: Meridian
reads its `mars.toml` only to decide whether it still holds items, and if so passes it as
`--fallback-root` and warns once per process with the steps to move it
(`LegacyUserRoot.migration_message`): each dependency as the `meridian mars --user add` that
recreates it with its version and filters, or, for a key `add` can't set, the entry to copy;
`[settings]`/`[models]`/`[agents]`/`[skills]` to Mars's user `config.toml`. A `mars.toml` it
can't read stays active so Mars reports it, and the warning says it can't be read. Removal is
in `TODO`.

## Models

Meridian defines no builtin model aliases. Mars resolution fails loudly when
Mars cannot run; alias inventory is then empty. Inventory is static, not live routing availability. Mars subprocesses allow 60
seconds because a cold models.dev refresh can be slow.

Cache-only and normal alias lookups have separate cache keys so a dry run cannot
suppress a later refresh within the same operation. Missing results are cached
for the operation as well.

Mars can prefix-match a literal model ID to a different ID. The direct resolution
helper checks the catalog for an exact literal match before accepting that result;
the catalog proves identity, not a harness route. Static aliases and exact-ID
matches can legitimately lack a harness.

## Agent profiles

`model-invocable` controls default subagent visibility, not whether a profile
can be loaded. Missing or non-boolean values default to true for compatibility.
See the [agent catalog](../../launch/.context/CONTEXT.md).

Meridian has no agent lookup of its own: Mars resolves every name, and Meridian parses
the file Mars chose (`parse_agent_profile` on the bundle's `agent_source_path`, or
`mars agents show` through `ops/mars.py` where no bundle is built). A second lookup could
pick a different file than the one that runs. Mars reads profile skills when building the
launch bundle for the same reason. Every by-name call to Mars sends `mars_content_args`
(`mars_cli.py`): the legacy `~/.meridian/mars` as `--fallback-root` while it holds items, then
this build's bundle directory as `--provided-root`. A missing agent is `MarsAgentNotFoundError` on every path (spawn,
`--fork`, configured default) and carries Mars's message; Meridian adds only a hint that
holds for every layer. Meridian never words its own guess at which layer was searched.

Built-in profiles (`meridian/mars/agents/`, today `inbox-router`) come from that provided
layer, which answers a name only when no installed agent claims it; a name Mars renamed on
a collision stays its `agent_renamed` error. They are read in place, never installed
into a store: an installed copy could go missing whenever another sync held the lock, or be a
stale one from another Meridian version. Meridian's skills likewise: `meridian skills` reads the
wheel only, launches read the bundle through `--provided-root`, and every passthrough command
in `MARS_SYNCING_COMMANDS` (Mars's list of commands that sync) hands the bundle to Mars as a
sync's host layer, for `.agy`.
