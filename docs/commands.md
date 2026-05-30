# CLI Reference

Full command surface. Use `--help` on any command for flags and options.

## Spawning & Monitoring

| Command | Description |
| ------- | ----------- |
| `meridian` | Launch the primary agent session with startup context, including the installed agent catalog |
| `meridian --continue REF` | Resume a prior primary session from a chat/session ref (`c123`), spawn ref (`p123`), or raw harness session id. Chat IDs are now the preferred reference — shown in the quit message when a primary session ends. |
| `meridian --fork [REF]` | Launch a new primary session by forking while preserving launch identity (agent/model/skills). `REF` accepts a spawn ref (`p123`), chat/session ref (`c123`), or raw harness session id. |
| `meridian --fork-fresh [REF]` | Launch a new primary session by forking and allowing launch identity changes (`-m`, `-a`). |
| `meridian --from [REF]` | Launch a fresh primary session with prior spawn or chat/session context as reference material only. `REF` defaults to `$MERIDIAN_SPAWN_ID` inside Meridian sessions. Does not fork transcript lineage. |
| `meridian bootstrap` | Launch a primary session with all installed bootstrap docs injected — guides first-time setup |
| `meridian spawn -a AGENT -p "task"` | Delegate work to a routed agent/model |
| `meridian spawn list` | See running and recent spawns |
| `meridian spawn list --profile reviewer` | Show spawns launched with the `reviewer` profile |
| `meridian spawn list --primary` | Show only primary spawns (top-level sessions) |
| `meridian spawn wait ID` | Block until a spawn completes; report body included by default, `--no-report` to suppress |
| `meridian spawn show ID` | Read a spawn's report and status |
| `meridian spawn status ID` | Read spawn status summary (report body off by default; add `--report` to include) |
| `meridian spawn --continue ID -p "more"` | Resume a prior spawn with new input |
| `meridian spawn --fork [REF] -p "next"` | Start a new spawn by forking while preserving launch identity (agent/model/skills) |
| `meridian spawn --fork-fresh [REF] -p "next"` | Start a new spawn by forking and allowing launch identity changes (`-m`, `-a`, `--skills`) |
| `meridian spawn --from [REF] -p "next"` | Start a new spawn with prior spawn or chat/session context (`REF` defaults to `$MERIDIAN_SPAWN_ID` inside Meridian sessions) |
| `meridian spawn cancel ID` | Cancel a running spawn |
| `meridian spawn cancel-all` | Cancel all running spawns in the current chat (or subtree when called from a nested spawn) |
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
| `--fork [REF]` | Fork into a new session while preserving source agent/model/skills (`REF` defaults to `$MERIDIAN_SPAWN_ID` inside Meridian sessions) |
| `--fork-fresh [REF]` | Fork into a new session and allow `-m`, `-a`, and `--skills` overrides (`REF` defaults to `$MERIDIAN_SPAWN_ID` inside Meridian sessions) |
| `--from [REF]` | Start a new spawn seeded with prior context from a spawn ref (`p123`) or chat/session ref (`c123`). `REF` defaults to `$MERIDIAN_SPAWN_ID` inside Meridian sessions. Does not fork transcript lineage. Also inherits the source's work item as a fallback (see Work Precedence below). |
| `--desc "label"` | Human-readable label in dashboards |
| `--work SLUG` | Attach to a specific work item (overrides ambient and --from inheritance) |
| `--task-dir PATH` | Override source-edit directory for this spawn/fork (must exist) |
| `--profile NAME` | `spawn list` only: filter by stored agent/profile name |
| `--primary` | `spawn list` only: include only `kind=primary` spawns |
| `--approval MODE` | `default` \| `confirm` \| `auto` \| `yolo` |
| `--metadata` | Show detailed inline accounting (model, cost, tokens, duration, report path). Still includes report body and transcript command. |
| `--no-report` | Suppress report body from output (default is now to show it) |
| `--verbose` | Debug/runtime verbosity |

`--fork` is identity-preserving by design. It rejects `-m`, `-a`, and `--skills`:

```text
--fork preserves launch identity. Use --fork-fresh to change agent, model, or skills.
```

`--fork-fresh` is the identity-changing variant. It is useful for role/model swaps, but can reduce prompt-cache locality because the profile/system prompt may change.

Session initiation modes: `--continue` resumes the same transcript, `--fork` branches with identity lock, `--fork-fresh` branches with identity overrides, and `--from [REF]` starts an independent transcript seeded by references only.

#### Work Precedence

When a spawn resolves which work item to attach to, precedence is:

1. **`--work`** (explicit) — always wins
2. **Ambient session** — the active work item of the current session
3. **`--from` inheritance** — the work item attached to the referenced spawn/session

`--from` only inherits work as a last-resort fallback. If you pass `--work`, or the current session already has an active work item, `--from` does not override it. This means you can safely use `--from` for context without accidentally pulling in a different work item.

### Spawn Output

Foreground `spawn` and single-spawn `spawn wait` default to compact output:
one-line status + report body + transcript pointer. Metadata (tokens, cost,
timestamps, paths, model) is hidden by default.

| Mode | Output |
|------|--------|
| Default (no format flag) | Compact text: status + report body + `Transcript: meridian session log <id>` |
| `--metadata` | Compact text + inline accounting (model, cost, tokens, duration, report path) |
| `--verbose` | Debug/runtime verbosity |
| `--no-report` | Status line only; report body suppressed |
| `--format json` | Structured JSON with report body and transcript command fields |
| `--bg` | Spawn ID + wait instructions (unchanged) |

`spawn show` and `spawn status` default to a moderate tier: status/model/duration plus actionable failure context.
Use `--verbose` to include internal diagnostics (tokens, lifecycle fields, backend metadata, and other internals).

Progressive disclosure:

```bash
meridian spawn -a reviewer -p "task"              # compact: status + report
meridian spawn -a reviewer -p "task" --metadata   # adds model/cost/tokens/path inline
meridian spawn show p123                           # moderate summary + report body
meridian spawn status p123                         # moderate summary without report body
meridian spawn show p123 --verbose                 # internal diagnostics view
meridian session log p123                          # recent transcript entries (last 5, chronological, safe content previews)
meridian session log p123 --full                   # full selected segment, including entry 0 prologue/handoff slot
meridian session log p123 --segment current --from 0 --limit 1  # read just segment prologue/handoff slot
meridian session log p123 --global --from 0 --limit 1           # read first entry in global cross-segment stream
meridian session log p123 --full --no-truncate     # full selected segment with full message content
```

Primary-session metadata from `primary_meta.json` (`kind`, `activity`,
`managed_backend`, `backend_pid`, `tui_pid`, `backend_port`,
`harness_session_id`, `session_config_dir`) is available in
`spawn show --verbose` and structured JSON output, not default moderate text.

`spawn cancel-all` scopes cancellation to the calling spawn's subtree when invoked
from inside a nested spawn (e.g., from an orchestrator agent). This prevents
accidentally cancelling sibling spawns or other parallel work running in the same
chat. Flags:

| Flag | Description |
| ---- | ----------- |
| `--work SLUG` | Cancel only spawns attached to a specific work item |
| `--include-primaries` | Also cancel primary (top-level) sessions |
| `--include-others` | Opt out of subtree scoping — cancel across the full chat (matches the behavior before subtree scoping was introduced) |

`meridian bootstrap` accepts the same launch flags as a primary session (`-m`, `--harness`, `-a`, `--work`, `--approval`, `--effort`, `--timeout`, `--dry-run`) plus setup flags (`--add`, `--link`).

When setup flags are present, bootstrap first runs the same setup flow as `meridian init --add/--link` (including Mars init when needed), then launches the guided bootstrap session with bootstrap docs injected. Setup flags cannot be combined with `--dry-run`.

The `-a` flag explicitly selects the bootstrap agent profile. If omitted, normal primary agent resolution applies (including `primary.agent` configured by package metadata during setup).

For managed Codex primary startup behavior, see [codex-tui-passthrough.md](codex-tui-passthrough.md).

## Reports & Sessions

| Command | Description |
| ------- | ----------- |
| `meridian spawn report show ID` | Show one spawn's report |
| `meridian spawn report search "query"` | Search across all spawn reports |
| `meridian session log REF` | Read conversation/progress logs for a chat, spawn, or harness session |
| `meridian session search "query" [REF]` | Search one session or a scoped session corpus (`--workspace`, `--global`, `--work`) |

## Work Items

| Command | Description |
| ------- | ----------- |
| `meridian work` | Dashboard — active work items and spawns |
| `meridian work start LABEL` | Create a work item if missing, or switch to it |
| `meridian work start LABEL --task-dir PATH` | Create/switch and set task directory for source edits |
| `meridian work list` | List all work items |
| `meridian work show SLUG` | Show one work item, its directory, and attached spawns |
| `meridian work switch SLUG` | Set active work item |
| `meridian work task-dir [PATH\|--clear]` | Show/set/clear active work-item task directory |
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
| `meridian chat` | Start the chat backend server (Claude, random port); serves frontend assets when available, falls back to headless |
| `meridian chat -m NAME` | Model id or alias; harness derived from the resolved model route |
| `meridian chat --harness NAME` | Explicit harness: `claude`, `codex`, `opencode`; must be compatible with model |
| `meridian chat -a AGENT` | Agent profile — same `.mars/agents/*.md` format as spawn |
| `meridian chat --skills SKILL` | Add skills (repeatable); merged after profile skills |
| `meridian chat --approval MODE` | Approval mode: `default`, `confirm`, `auto`, `yolo` |
| `meridian chat --yolo` | Shorthand for `--approval yolo` |
| `meridian chat --effort LEVEL` | Effort / reasoning level (harness-dependent) |
| `meridian chat --sandbox MODE` | Sandbox mode value (harness-dependent) |
| `meridian chat --autocompact PCT` | Enable autocompact at N% context use, integer percentage (harness-dependent) |
| `meridian chat --headless` | API-only mode; no frontend is started regardless of available assets |
| `meridian chat --frontend-dist PATH` | Path to pre-built frontend assets (`index.html` + `assets/`); errors if path has no valid assets |
| `meridian chat --dev` | Dev mode: Vite subprocess + verbose logging. Cannot combine with `--headless` or `--frontend-dist` |
| `meridian chat --dev --frontend-root PATH` | Path to `meridian-web` source checkout for dev mode (auto-discovered as `../meridian-web` if omitted) |
| `meridian chat --dev --open` | Open browser after server starts (`--open` ignored in `--headless`) |
| `meridian chat --dev --tailscale` | Share dev server on Tailscale network via portless |
| `meridian chat --dev --funnel` | Expose dev UI publicly via Tailscale Funnel (implies `--tailscale`) |
| `meridian chat --dev --no-portless` | Use raw Vite instead of portless; cannot combine with `--tailscale`/`--funnel` |
| `meridian chat --dev --portless-force` | Take over an occupied portless dev route at startup |
| `meridian chat --port PORT` | Bind to a fixed port (`0` = auto-assign) |
| `meridian chat --host HOST` | Bind interface (default `127.0.0.1`) |
| `meridian chat ls` | List chats on the running server (runtime management only) |
| `meridian chat show CHAT_ID` | Show state and recent events for a chat |
| `meridian chat log CHAT_ID` | Print event log; `--follow` tails live |
| `meridian chat close CHAT_ID` | Close a chat conversation |

Launch-policy flags (`-m`, `--harness`, `-a`, `--skills`, `--approval`, etc.) are
resolved once at startup and persisted per chat. A harness/model conflict fails before
the server binds a port. Management subcommands (`ls`, `show`, `log`, `close`) do not
accept launch-policy flags — they are runtime management only.

`--timeout` is not supported for `meridian chat`.

The server exposes REST endpoints and a bidirectional WebSocket for creating chats,
sending prompts, streaming normalized `ChatEvent` frames, handling HITL approvals
(Codex), and reverting to git checkpoints.

See [chat.md](chat.md) for the full API reference including launch policy resolution,
event types, command types, reconnect/replay, persistence, and harness support matrix.

## Configuration & Diagnostics

| Command | Description |
| ------- | ----------- |
| `meridian init [--add SOURCE] [--link DIR]` | Initialize project config/runtime state; optional Mars package install/link setup |
| `meridian workspace init` | Create or update local `[workspace]` examples in `meridian.local.toml` |
| `meridian config show` | Show resolved configuration |
| `meridian config set KEY VALUE` | Set a config value |
| `meridian config get KEY` | Read a config value |
| `meridian config reset KEY` | Reset a config value to default |
| `meridian mars models list` | Inspect the model catalog |
| `meridian mars models refresh` | Force-refresh the models.dev cache |
| `meridian doctor` | Per-project diagnostics and orphan reconciliation (cheap, safe to run anywhere) |
| `meridian doctor --global` | Adds the machine-wide orphan-project-dir scan (`~/.meridian/projects/*`) to the normal current-project doctor checks; must run from the root process (not inside a spawn) |
| `meridian doctor --prune` | Prune stale spawn artifacts and telemetry retention targets in the current project |
| `meridian doctor --prune --global` | Same as `--prune`, plus orphan project dirs machine-wide |
| `meridian serve` | Start the MCP server |

`meridian doctor` scans for three categories of stale state and reports them as distinct warning codes:

| Warning code | What it means |
| ------------ | ------------- |
| `stale_spawn_artifacts` | Spawn artifact directories for completed spawns past the retention window |
| `stale_telemetry_segments` | Current-project telemetry segments that would be pruned by retention cleanup because they are expired or because total telemetry size exceeds the cap |
| `stale_orphan_project_dirs` | Project state directories with no matching live project (`--global` only) |

Local `--prune` removes stale spawn artifacts and telemetry segments selected by current-project retention cleanup. Telemetry cleanup first removes expired non-live segments, then may remove older closed segments to enforce the size cap. Orphan project dirs are only pruned by `meridian doctor --prune --global`.

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

Mars config (`mars.toml`, plus local Mars overlays) owns package dependencies,
targets, model aliases/catalog settings, project routing defaults
(`default_model`, `default_harness`), and `[agents.<name>]` runtime overlays.
Meridian config (`meridian.toml`, `meridian.local.toml`, and user config) owns
CLI/runtime behavior such as timeouts, output, state, work/context/workspace,
hooks, harness defaults, and primary-session defaults.

`meridian init` (no setup flags) bootstraps Meridian config/runtime only.

`meridian init --add ...` runs setup flow:
- initializes Mars when `mars.toml` is missing
- installs package sources
- links requested targets (or package-declared defaults)
- applies package-declared `primary.agent` when config is unset

`meridian init --link DIR` without `--add` is still a top-level convenience path:
- without `mars.toml`, it shells through `meridian mars init --link DIR`
- with `mars.toml`, it shells through `meridian mars link DIR`

`meridian bootstrap --add ... --link ...` runs this same setup flow, then launches a guided bootstrap primary session.

`meridian mars sync` automatically sets `MERIDIAN_MANAGED=1` in the mars subprocess environment. Mars uses this signal to suppress native agent emission to harness directories — agents are read by Meridian from `.mars/agents/`, not duplicated into `.claude/agents/` etc.

See [agent-profiles.md](agent-profiles.md) for the agent profile format including `model-policies`, `no-fallback`, and `mode`. Legacy `fanout` and `fallback-order` are rejected; migrate to `model-policies` list order + `no-fallback: true`.

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
