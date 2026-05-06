# CLI Reference

Full command surface. Use `--help` on any command for flags and options.

## Spawning & Monitoring

| Command | Description |
| ------- | ----------- |
| `meridian` | Launch the primary agent session with startup context, including the installed agent catalog |
| `meridian bootstrap` | Launch a primary session with all installed bootstrap docs injected — guides the agent through first-time environment setup |
| `meridian spawn -a AGENT -p "task"` | Delegate work to a routed agent/model |
| `meridian spawn list` | See running and recent spawns |
| `meridian spawn list --primary` | Show only primary spawns (top-level sessions) |
| `meridian spawn wait ID` | Block until a spawn completes |
| `meridian spawn show ID` | Read a spawn's report and status |
| `meridian spawn --continue ID -p "more"` | Resume a prior spawn with new input |
| `meridian spawn --from REF -p "next"` | Start a new spawn with prior spawn or chat/session context |
| `meridian spawn cancel ID` | Cancel a running spawn |
| `meridian spawn inject ID --message "text"` | Inject a message into a running streaming spawn |
| `meridian spawn stats` | Aggregate spawn statistics |
| `meridian spawn children ID` | List direct child spawns |
| `meridian spawn files ID` | List files changed by a spawn |

Common `spawn` flags:

| Flag | Description |
| ---- | ----------- |
| `-a AGENT` | Agent profile to use |
| `-m MODEL` | Model override |
| `-p "prompt"` | Inline prompt |
| `--prompt-file PATH` | Read prompt from file |
| `-f FILE` | Attach context file (repeatable) |
| `--from REF` | Attach prior context from a spawn ref (`p123`) or chat/session ref (`c123`) |
| `--desc "label"` | Human-readable label in dashboards |
| `--work SLUG` | Attach to a specific work item |
| `--primary` | `spawn list` only: include only `kind=primary` spawns |
| `--approval MODE` | `default` \| `confirm` \| `auto` \| `yolo` |

`spawn show` includes primary-session metadata when available from `primary_meta.json`:
`kind`, `activity`, `managed_backend`, `backend_pid`, `tui_pid`, `backend_port`,
and `harness_session_id`.

`meridian bootstrap` accepts the same launch flags as a primary session (`-m`, `--harness`, `-a`, `--work`, `--approval`, `--effort`, `--timeout`, `--dry-run`). Bootstrap docs are injected automatically — no extra flags needed. The `-a` flag selects the agent profile; if omitted, Meridian uses the default bootstrap agent from the installed catalog.

For managed Codex primary startup behavior, see [codex-tui-passthrough.md](codex-tui-passthrough.md).

## Reports & Sessions

| Command | Description |
| ------- | ----------- |
| `meridian spawn report show ID` | Show one spawn's report |
| `meridian spawn report search "query"` | Search across all spawn reports |
| `meridian session log REF` | Read conversation/progress logs for a chat, spawn, or harness session |
| `meridian session search "query" REF` | Search session transcripts |

## Work Items

| Command | Description |
| ------- | ----------- |
| `meridian work` | Dashboard — active work items and spawns |
| `meridian work start LABEL` | Create a work item if missing, or switch to it |
| `meridian work list` | List all work items |
| `meridian work show SLUG` | Show one work item, its directory, and attached spawns |
| `meridian work switch SLUG` | Set active work item |
| `meridian work done SLUG` | Mark a work item done and archive its scratch directory |
| `meridian work sessions SLUG` | List sessions attached to a work item |

## Hooks

| Command | Description |
| ------- | ----------- |
| `meridian hooks list` | Show all registered hooks |
| `meridian hooks check` | Validate hook configuration |
| `meridian hooks run NAME` | Execute a hook manually, bypassing interval throttling |
| `meridian hooks run NAME --event EVENT` | Execute with a specific event context |

See [hooks.md](hooks.md) for event names, builtin hooks, and hook configuration schema.

## Context

| Command | Description |
| ------- | ----------- |
| `meridian context` | Show all resolved context paths |
| `meridian context work` | Print the absolute path for the `work` context |
| `meridian context kb` | Print the absolute path for the `kb` context |
| `meridian context work.archive` | Print the absolute path for the `work.archive` context |
| `meridian context NAME` | Print the absolute path for any configured named context |
| `meridian context --verbose` | Show source, path, and resolved details for each context |

```bash
meridian context           # show all resolved context paths
meridian context work      # print just the work path
meridian context strategy  # print a configured arbitrary context path
meridian context --verbose # show source and resolution details
```

Context paths can be backed by a local directory (default) or a remote Git repo (cloned and resolved at runtime). `work` and `kb` are built in; additional `[context.NAME]` tables are arbitrary named contexts. Configure in `meridian.toml`:

```toml
[context.work]
source = "git"
remote = "git@github.com:team/docs.git"
path   = "project/work"
archive = "project/archive/work"

[context.kb]
source = "git"
remote = "git@github.com:team/kb.git"
path   = "knowledge"

[context.strategy]
source = "git"
remote = "git@github.com:team/docs.git"
path   = "project/strategy"
```

See [configuration.md](configuration.md#context) for the full schema.

## Extensions

| Command | Description |
| ------- | ----------- |
| `meridian ext list` | List registered extensions grouped by namespace |
| `meridian ext show EXT_ID` | Show commands in one extension |
| `meridian ext commands` | List all extension commands; `--json` for stable agent output |
| `meridian ext run FQID` | Invoke an extension command; app-server-backed commands currently report no server |

`FQID` is `extension_id.command_id`, e.g. `meridian.sessions.getSpawnStats`.

`ext list`, `ext show`, and `ext commands` work with no app server running. `ext run` runs in-process for commands with `requires_app_server: false`; commands with `requires_app_server: true` currently return exit code `2` while the app server is archived for rebuild.

Common `ext run` flags:

| Flag | Description |
| ---- | ----------- |
| `--args JSON` | JSON object of args for the command (default `{}`) |
| `--work-id ID` | Work item context |
| `--spawn-id ID` | Spawn context |
| `--request-id ID` | Tracing request ID |
| `--json` | Output as JSON (alias for `--format json`) |

Exit codes for `ext run`: `2` = app server unavailable, `7` = invalid `--args`.

See [extensions.md](extensions.md) for HTTP API and MCP tool details.

## Chat Backend

| Command | Description |
| ------- | ----------- |
| `meridian chat` | Start the headless chat backend server (Claude, random port) |
| `meridian chat --harness NAME` | Use a specific harness: `claude`, `codex`, `opencode` |
| `meridian chat --model NAME` | Override model |
| `meridian chat --port PORT` | Bind to a fixed port (`0` = auto-assign) |
| `meridian chat --host HOST` | Bind interface (default `127.0.0.1`) |

The server exposes REST endpoints and a bidirectional WebSocket for creating chats,
sending prompts, streaming normalized `ChatEvent` frames, handling HITL approvals
(Codex), and reverting to git checkpoints.

See [chat.md](chat.md) for the full API reference including event types, command types,
reconnect/replay, persistence, and harness support matrix.

## Configuration & Diagnostics

| Command | Description |
| ------- | ----------- |
| `meridian init [--link DIR]` | Initialize project config/runtime state; optional convenience link wiring via mars |
| `meridian workspace init` | Create or update local `[workspace]` examples in `meridian.local.toml` |
| `meridian workspace migrate` | Convert legacy `workspace.local.toml` roots to `[workspace.NAME]` entries in `meridian.local.toml` |
| `meridian workspace migrate --force` | Replace existing local `[workspace]` entries while migrating legacy roots |
| `meridian config show` | Show resolved configuration |
| `meridian config set KEY VALUE` | Set a config value |
| `meridian config get KEY` | Read a config value |
| `meridian config reset KEY` | Reset a config value to default |
| `meridian mars models list` | Inspect the model catalog |
| `meridian models refresh` | Force-refresh the models.dev cache |
| `meridian doctor` | Per-project diagnostics and orphan reconciliation (cheap, safe to run anywhere) |
| `meridian doctor --global` | Cross-project scan — checks all projects under `~/.meridian/`; must run from the root process (not inside a spawn) |
| `meridian doctor --prune` | Prune stale spawn artifacts and Claude overlay directories (current project); attempts transcript materialization before overlay deletion |
| `meridian doctor --prune --global` | Same as `--prune`, plus orphan project dirs machine-wide |
| `meridian serve` | Start the MCP server |

`meridian doctor` scans for three categories of stale state and reports them as distinct warning codes:

| Warning code | What it means |
| ------------ | ------------- |
| `stale_spawn_artifacts` | Spawn artifact directories for completed spawns past the retention window |
| `stale_claude_overlays` | Per-spawn Claude config overlay directories past the retention window; transcripts are still intact inside them until pruned |
| `orphan_project_dirs` | Project state directories with no matching live project (`--global` only) |

`--prune` removes all three. For Claude overlays specifically, Meridian makes a best-effort attempt to materialize any remaining session transcript files into `~/.claude/projects/` before deleting the overlay. If materialization fails, Meridian logs a warning and continues prune handling. See [troubleshooting.md](troubleshooting.md#claude-session-isolation) for details on the overlay model.

## Telemetry

Meridian writes structured telemetry events to per-process JSONL segment files.
Segments live under the project's runtime directory:

```
<project_runtime_root>/telemetry/<owner>.<pid>-<seq>.jsonl
```

The `<owner>` component is the logical writer (`cli`, `chat`, or a spawn ID).
`<pid>` is the OS process ID. `<seq>` is a per-process rotation counter. You
see these filenames in `status` output.

| Command | Description |
| ------- | ----------- |
| `meridian telemetry tail` | Live-stream telemetry events from the current project |
| `meridian telemetry query` | Print historical events from the current project as JSON lines |
| `meridian telemetry status` | Show segment health, active writers, and storage size |

Common flags available on `tail`, `query`, and `status`:

| Flag | Description |
| ---- | ----------- |
| `--global` | Read from all projects instead of just the current one |

Additional `query` flags:

| Flag | Description |
| ---- | ----------- |
| `--since DURATION` | Only include events newer than a duration, e.g. `1h`, `30m` |
| `--limit N` | Cap output at N events |

Filtering flags available on `tail` and `query`:

| Flag | Description |
| ---- | ----------- |
| `--domain DOMAIN` | Filter by telemetry domain |
| `--spawn ID` | Filter by spawn ID |
| `--chat ID` | Filter by chat ID |
| `--work ID` | Filter by work item ID |

**Cross-project queries.** Use `--global` to aggregate across every project under
`~/.meridian/projects/`. This is the only way to reach telemetry from projects
other than the one you're currently inside.

```bash
meridian telemetry tail --global                     # stream all projects
meridian telemetry query --global --since 1h         # last hour across all projects
meridian telemetry status --global                   # storage summary for all projects
```

**Legacy segments.** Segments written by versions prior to the per-project
storage change live at `~/.meridian/telemetry/`. They are read-only (nothing
writes there anymore), visible via `--global`, and age out automatically through
the normal retention policy (7 days / 100 MB). `status` reports a legacy count
when any remain.

**Rootless processes.** The MCP stdio server runs without a project root and
cannot write to a project telemetry directory. It emits telemetry to stderr
only. Those events are not visible through `tail`, `query`, or `status`.

## Package Management (mars)

| Command | Description |
| ------- | ----------- |
| `meridian mars init [--link DIR]` | Initialize mars project (`mars.toml`) and optionally create the initial link target in the same command |
| `meridian mars add SOURCE` | Add an agent/skill package source |
| `meridian mars sync` | Compile packages into `.mars/`; emit skills to native harness dirs |
| `meridian mars link DIR` | Link compiled output into a harness tool directory |
| `meridian mars list` | Show installed agents (grouped by mode) and skills |
| `meridian mars upgrade` | Fetch latest versions and sync |
| `meridian mars doctor` | Check for drift and integrity issues |

`meridian init --link DIR` is the top-level convenience path:
- without `mars.toml`, it shells through `meridian mars init --link DIR`
- with `mars.toml`, it shells through `meridian mars link DIR`

`meridian mars sync` automatically sets `MERIDIAN_MANAGED=1` in the mars subprocess environment. Mars uses this signal to suppress native agent emission to harness directories — agents are read by Meridian from `.mars/agents/`, not duplicated into `.claude/agents/` etc.

See [agent-profiles.md](agent-profiles.md) for the agent profile format including `model-policies`, `fanout`, and `mode`.

## Spawn Statuses

| Status | Meaning |
| ------ | ------- |
| `queued` | Registered but harness not yet started |
| `running` | Harness process is active |
| `finalizing` | All post-exit work is done; runner is committing the terminal state — no new work will happen, but the spawn is not yet terminal |
| `succeeded` | Completed successfully |
| `failed` | Completed with an error |
| `cancelled` | Cancelled before or during execution |

`queued`, `running`, and `finalizing` are active (in-flight) statuses. They all count toward active spawn counts in `spawn list` and the `work` dashboard. `finalizing` is typically brief — a few seconds at most — but is visible between harness exit and final persistence.

## Spawn References

Several commands accept symbolic spawn references in addition to literal IDs:

| Reference | Resolves to |
| --------- | ----------- |
| `@latest` | Most recently created spawn |
| `@last-failed` | Most recent spawn with status `failed` |
| `@last-completed` | Most recent spawn with status `succeeded` |
