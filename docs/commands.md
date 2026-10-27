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
| `meridian --task-dir PATH` | Override the source-edit directory for this primary launch only. Does not modify the work item's `task_dir` setting. Relative `-f` paths resolve against this directory. Rejected with `--continue` — use `--fork --task-dir` to diverge. |
| `meridian spawn -a AGENT -p "task"` | Delegate work to a routed agent/model |
| `meridian spawn list` | See running and recent spawns |
| `meridian spawn list --profile reviewer` | Show spawns launched with the `reviewer` profile |
| `meridian spawn list --primary` | Show only primary spawns (top-level sessions) |
| `meridian spawn wait` | Wait for this session's running descendants and return up to 32 unconsumed finished background children as lookup headers; children waited to completion retain full reports; on return, list children that appeared while waiting without consuming them; yields a retry checkpoint if concurrent history writes delay discovery |
| `meridian spawn wait ID` | Block until a spawn completes; report body included by default, `--no-full` to suppress |
| `meridian spawn show ID` | Show a spawn's report and status; inside a spawn, a terminal report body consumes its pending completion notice (`--no-observe` opts out) |
| `meridian spawn status ID` | Read spawn status summary; `--full` includes and consumes a terminal report body unless combined with `--no-observe` |
| `meridian spawn --continue ID -p "more"` | Resume a prior spawn with new input |
| `meridian spawn --fork [REF] -p "next"` | Start a new spawn by forking while preserving launch identity (agent/model/skills) |
| `meridian spawn --fork-fresh [REF] -p "next"` | Start a new spawn by forking and allowing launch identity changes (`-m`, `-a`, `--skills`) |
| `meridian spawn --from [REF] -p "next"` | Start a new spawn with prior spawn or chat/session context (`REF` defaults to `$MERIDIAN_SPAWN_ID` inside Meridian sessions) |
| `meridian spawn cancel ID` | Cancel a running spawn |
| `meridian spawn cancel-all` | Cancel all running spawns in the current chat (or subtree when called from a nested spawn) |
| `meridian spawn message ID --message "text"` | Post a message to a running streaming spawn or supported primary inbox |
| `meridian spawn stats` | Aggregate spawn statistics |
| `meridian spawn children [ID]` | List direct child spawns (defaults to this spawn) |

OpenCode managed primaries honor explicit provider/model selections without editing
native config. See [managed startup and failure behavior](codex-tui-passthrough.md#failure-behavior).
`spawn message` uses the control endpoint for spawned sessions. For a live Claude
primary, it posts to Claude Code's session inbox; the command confirms only that
the message was posted, not delivered. Claude holds the message for human approval
when that session bypasses permission prompts and `crossSessionInbound` is not
`accept`; to skip the prompt, set `"crossSessionInbound": "accept"` in
`~/.claude/settings.json`. This applies to every peer message, not only
Meridian's; [Claude's inbound gate](harness-integration.md#primary-message-delivery-and-claudes-inbound-gate)
covers the values and the trade-off. Each injected
message opens with a `[Meridian]` line naming the sending spawn and, when the
sender can receive one, the `meridian spawn message <id>` command that answers
it. Other primary harnesses
fail immediately because they do not yet expose a message route
([#498](https://github.com/haowjy/meridian-cli/issues/498)).

Common `spawn` flags:

No-arg `spawn wait` gives a missing history index its own sticky cold-build budget;
the wait deadline then bounds one catch-up attempt. If the history index is still
busy, the command exits cleanly with a discovery-pending checkpoint. Re-run it; it
does not print "No pending spawns" until discovery has complete coverage.
Candidate lookup is scoped to the current chat in the index, or to the caller's
recursive subtree for nested spawns, before selected rows are checked against
authoritative spawn state. Already-finished owed children are selected oldest first,
at most 32 per invocation, and render with the same status/worker/failure/task-dir
header and `meridian spawn show <id>` lookup as `spawn collect`. Running targets are
not capped; after the wait blocks for them, their normal report output is preserved.
In structured output, blocked-on results are in `spawns` and already-finished owed
results are in `owed_spawns`; the latter omit `report_body` even with `--full`.
Children discovered only when the wait returns are in `since_started_spawns`; they
are advisory and do not affect counts, fail-fast, observation, or the exit code.
`--no-observe` uses the same rendering and cap without consuming any result.

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
| `--approval MODE` | `default` \| `confirm` \| `auto` \| `never`. Overrides the profile; each harness applies only some modes: [Permissions by harness](agent-profiles.md#permissions-by-harness) |
| `--sandbox MODE` | `default` \| `read-only` \| `workspace-write` \| `danger-full-access`. Overrides the profile; harness support differs |
| `--network MODE` | `allow` \| `deny` \| `allowlist`. Overrides the profile mode for this launch; shell network only |
| `--network-allow HOST` | Add an exact host, IP, `*.x`, or `**.x` to the allow set (repeatable) |
| `--network-deny HOST` | Add a host pattern to the deny set (repeatable; denies win) |
| `--yolo` | Shorthand for `--approval never`. Rejected together with `--approval` |
| `--metadata` | Show detailed inline accounting (model, cost, tokens, duration, report path). Still includes report body and transcript command. |
| `--no-full` | Suppress report body from output (default is now to show it) |
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
| `--no-full` | Status line only; report body suppressed |
| `--format json` | Structured JSON with report body and transcript command fields |
| `--bg` | Spawn ID + wait instructions (unchanged) |

`spawn show` and `spawn status` default to a moderate tier: status/model/duration plus actionable failure context.
Inside a spawn, printing a non-empty terminal report through `spawn show`,
`spawn status --full`, or `spawn report show` consumes its pending completion
notice. `spawn cancel` likewise consumes an outcome it reports as `cancelled`.
Pass `--no-observe` to any of those commands to leave the result owed. Piped or
redirected output still counts as shown; use
`--no-observe` when another process, UI, or file is the actual recipient.
An invocation directed through `-C` never observes: show, full status, report show,
cancel, cancel-all, and wait write no consumption record, while ack is refused.
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
`session_config_dir`) is available in `spawn show --verbose` and structured
JSON output, not default moderate text. JSON for `spawn show`, `status` and
`wait` always carries the chat identity: `chat_id` (the entry chat),
`continue_chat_id` (a finished run's verified exit chat, otherwise the entry
chat) and `run_boundary` (`null` when absent). The native harness session ID is
not in JSON; `spawn show --verbose` prints it for primaries. Resolve native
keys from the chat.

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
| `meridian spawn report show ID` | Show one spawn's report; inside a spawn, a non-empty terminal report consumes its pending completion notice (`--no-observe` opts out) |
| `meridian spawn report search "query"` | Search across all spawn reports |
| `meridian session browse [--status active\|archived] [--sort updated\|created]` | Browse sessions (Space opens a transcript, `l` the fork/`--from` lineage, Tab the Status and Sort filters); `/` searches the bound native transcripts of the rows the Status view shows, never ZIP contents |
| `meridian session log REF` | Read a chat, spawn, harness session, or ZIP-backed history UUID |
| `meridian session search "query" [REF]` | Search one session or a scoped corpus (`--workspace`, `--global`, `--work`); includes bound archived chats by default, never runner history |
| `meridian session index status` | Inspect metadata and native-search coverage without rebuilding |
| `meridian session index rebuild [--reset] [--metadata-only]` | Rebuild discovery metadata and native search from authoritative files and available ZIPs; `--reset` repairs dirty-source coordination; `--metadata-only` skips native-search rebuild |
| `meridian session archive REF... --destination PATH` | Plan retention of selected histories; add `--apply` to verify and reclaim loose copies |
| `meridian session archive --eligible --destination PATH` | Plan retention by last activity (default 30 days); supports `--after-days` and `--apply` |
| `meridian session archive --prune-runner-history [--apply] [--after-days N]` | Explicitly prune redundant runner-stream files (dry-run by default; age defaults to 14 days) |
| `meridian session archive --list` | Show current/snapshot ZIPs and their registered locations |
| `meridian session import ZIP` | Select a verified transferred ZIP snapshot for direct reads |
| `meridian session restore REF... --archive ZIP_OR_UUID` | Restore only selected histories with fresh inert local aliases; preserve the ZIP |
| `meridian session repair REF` | Read-only: for an unbound chat (`c123`, or its spawn `p123`), list candidate native session files with evidence and the exact bind command; for a bound chat, show its binding |
| `meridian session repair REF --native PATH [--force]` | Bind an unbound chat to that native session file after validation; `--force` accepts a cwd mismatch or a start outside the chat's time window |

Archive destinations can also come from configuration. Automation is off by
default; active records and dependencies cannot be reclaimed. Published ZIPs
are never automatically deleted. UI visibility archive is separate from ZIP
retention. Search reports unavailable content or budget exhaustion as incomplete,
not a clean negative. See [History storage and retention](history.md) for repair,
configuration, transfer, conflict behavior and archive-integrity limits.

`session repair` never rebinds: bindings are immutable. `--native` always refuses
a bound chat, a file that is not a valid native session for the chat's harness,
and a session already bound to another chat. Candidate listing covers Pi (the
chat's spawn session dir; the shared root for primaries) and Claude (the recorded
project store); for Codex and OpenCode pass `--native`. An OpenCode database holds
many sessions, so it binds only a chat that already records its session ID.

Runner-history pruning is explicit, never automatic; see
[Prune redundant runner streams](history.md#prune-redundant-runner-streams).

## Work Items

| Command | Description |
| ------- | ----------- |
| `meridian work` | Dashboard — active work items and spawns |
| `meridian work current` | Print the active work scope directory (a primary session's attached work item, otherwise its launch-bound or ambient spawn scope) |
| `meridian work path RELPATH` | Materialize a path under the active scope and print its absolute path (creates parent dirs) |
| `meridian work root` | Print the configured work root (context tree for named work items) |
| `meridian work start LABEL` | Create a work item if missing, or switch to it |
| `meridian work start LABEL --task-dir PATH` | Create/switch and set task directory for source edits |
| `meridian work list` | List all work items |
| `meridian work show [SLUG]` | Show one work item, its directory, and attached spawns (default: this session's active work item) |
| `meridian work switch SLUG\|current` | Set active work item |
| `meridian work task-dir [PATH\|--clear]` | Show/set/clear active work-item task directory |
| `meridian work update [SLUG] --status/--desc/--goal` | Update a work item (default: this session's active work item) |
| `meridian work done [SLUG]` | Mark a work item done and archive its scratch directory (default: this session's active work item) |
| `meridian work sessions [SLUG]` | List sessions attached to a work item (default: this session's active work item) |
| `meridian work rename [OLD] NEW` | Rename a work item; one argument renames this session's active item, like `git branch -m NEW` |
| `meridian work delete [SLUG\|current]` | Delete a work item (default: this session's active work item) |
| `meridian work reopen SLUG\|current` | Reopen a done work item |

Commands that act on a work item default to the one this session is attached to,
and `current` names it anywhere a work id is accepted (`work show`, `sessions`,
`done`, `update`, `rename`, `delete`, `switch`, `reopen`, `spawn --work`), so no
script has to carry an id that a `work switch` can invalidate. `current` is
reserved: `work start`/`rename` refuse it as a name.

`work done` moves the item's directory to the archive immediately and writes no
session state: every attached execution keeps its attachment, and the item's own
links follow the directory, so a spawn that is still running keeps writing through
the same literal path and its writes land in the archive. A primary attached to the
item keeps it, and `work switch` to a done item reopens it. `work rename` moves the
directory and moves the executions that are live and attached to it onto the new
name. A chat that already ended keeps the name it ended on, so `--from` on it warns
that the item no longer exists and creates nothing; pass `--work` to name one. A
`work rename` that stopped part way is finished by running it again with the same two
names; with a name that was never renamed to the new one, it reports that the item is
not found. A done item keeps its checkout, so `meridian task-dir` and the `task` link
still name it. `work delete` returns the live executions to their ambient directory.
`work reopen` moves the directory back, and `work start` or `work switch` of a done
item reopens it.

A move never deletes either copy. If a directory exists in both places — an
interrupted operation, or a leftover — it is refused with both paths named, and
nothing is moved or deleted.

Every live execution, primary or spawn, has two stable symlinks under the runtime
root that keep moving targets out of the prompt and environment. Each points at a
link the work store keeps for the item (`work-items/<id>/work` and `task`), so a move
of the item needs no write to any session:

- `sessions/<chat>/work` (`MERIDIAN_ACTIVE_WORK_DIR`) is the work directory.
  `work start`, `work switch`, `work rename` and `work delete` repoint it, and a
  `work done` or `work reopen` moves the item under it; with no active item it
  points at the execution's ambient spawn work directory. Agents can write prompt
  files with that literal path and pass it to `--prompt-file` without resolving the
  work path again. The link follows the current item, so write a prompt after the
  final switch; a path saved before a switch still names the same link but reaches
  the new item.
- `sessions/<chat>/task` (`MERIDIAN_TASK_DIR`) is the source checkout: the execution's
  pinned checkout, else its item's checkout, else the project root. It follows
  `meridian task-dir`, `meridian work task-dir`, and a switch to an item with its own
  checkout, so the same literal path keeps reaching the current one. Three things
  pin a checkout to one execution, and `meridian task-dir clear` removes the pin: a
  launch's `--task-dir`; a launch from a directory outside the project, which counts
  as `--task-dir`; and the pin of the execution that launched it. A child of an
  execution that follows its item or the project root inherits no pin, so it follows
  `work task-dir` and `work switch` too.

The chat in the path is the execution's own: a primary's entry chat (the one its
launch named, which survives a `/clear` rotation) and a spawn's own chat. A primary
launch names its links in the system prompt; a spawn names them in its first user
turn, so sibling spawns share one cached system prompt.

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

See [configuration.md](configuration/context.md) for the full schema.

## Extensions

| Command | Description |
| ------- | ----------- |
| `meridian ext list` | List registered extensions grouped by namespace |
| `meridian ext show EXT_ID` | Show commands in one extension |
| `meridian ext commands` | List all extension commands; `--json` for stable agent output |
| `meridian ext run FQID` | Invoke an extension command in process |

`FQID` is `extension_id.command_id`, e.g. `meridian.mermaid.check`.

`ext list`, `ext show`, `ext commands`, and `ext run` do not require a separate
app server.

Common `ext run` flags:

| Flag | Description |
| ---- | ----------- |
| `--args JSON` | JSON object of args for the command (default `{}`) |
| `--work-id ID` | Work item context |
| `--spawn-id ID` | Spawn context |
| `--request-id ID` | Tracing request ID |
| `--json` | Output as JSON (alias for `--format json`) |

Exit codes for `ext run`: `1` = command or invocation error, `7` = invalid `--args`.

See [extensions.md](extensions.md) for the shared CLI and MCP contract.

## Notifications & Idle Sessions

### `meridian notify`

```text
meridian notify "<title>" ["<body>"] [--attach <path>]... [--no-email] [--priority 1..5] [--json]
```

Fires the `notify.manual` hook event, which the built-in `notify` hook delivers
through the configured push and email backends unless config routes it
elsewhere. The title is one line: what the human needs to decide or see. If the
first argument contains several lines, the first non-empty line becomes the
message in the title and the remaining lines are prepended to the
optional body. The title starts with the sender (tmux session, agent and harness),
followed by the message; the body ends with the sender and its current work item.
`--no-email` keeps it
off the email backends (`smtp`, `gmail`), however a route names them.

`--attach <path>` (repeatable) sends a file with it: email attaches every file
to the one message; ntfy, which carries one file per message, sends the text
and then each file as its own lower-priority message (the server must allow
attachments, and sets the size limit). Command hooks get the absolute paths in
`notification.attachments`. A missing or unreadable path fails with exit 2
before any route runs. Repeating paths that resolve to the same file sends that
file once. Email attachments are all-or-nothing: if the email provider rejects
one file (for example, for size), that email and its text fail together. If
ntfy accepts the text but rejects files, it still attempts every remaining file;
the route remains sent and warns with every failed file name, and the
notification text is not retried.

Results list one entry per route, named by its hook (`notify` for the default
route) plus its successful transports: text renders `notify (ntfy)`, while
`--json` has a `transports` list per route and adds the session `label`. A
failed route's error names the transports that failed. Exit 0 when something delivered, when every route
delivered nothing (backends `none`), or when no route ran (with the warning "no
notification route ran for notify.manual"); exit 1 when a route failed and none
delivered. No in-process retry.

### `meridian idle`

`idle` is an adapter-facing command group. Harness mods and launcher tasks use
it to report turn ends and user returns, and to ask core whether a due push or
cache warning should still be sent. Humans normally use only:

| Command | Description |
|---|---|
| `meridian idle status [--json]` | List open idle stretches and their stored schedules |
| `meridian idle mod-path` | Print the bundled Claude idle mod directory for an external-TUI opt-in |

The adapter subcommands are `config`, `arm`, `return`, `needs-permission`,
`fire`, and `event`; run `meridian idle --help` for their typed contract.
Outside-Meridian adapters must pass `--interactive` on every call. An explicit
spawn role stays disabled even with that flag.

See [configuration.md](configuration.md#notifications-and-idle-sessions) for
notification backends, [routing and extension channels](configuration.md#routing-and-extending-notifications), idle timers, how running work affects the push,
precedence, per-harness defaults, and the Claude opt-in command.

## Configuration & Diagnostics

| Command | Description |
| ------- | ----------- |
| `meridian init [--add SOURCE] [--link DIR]` | Initialize project config/runtime state; optional Mars package install/link setup |
| `meridian workspace init` | Create or update local `[workspace]` examples in `meridian.local.toml` |
| `meridian config show` | Show resolved configuration |
| `meridian config set KEY VALUE` | Set a config value |
| `meridian config get KEY` | Read a config value |
| `meridian config reset KEY` | Reset a config value to default |
| `meridian mars models list` | Inspect curated harness-model possibilities (human view) |
| `meridian mars models aliases --json` | Read the static alias inventory |
| `meridian mars models catalog --json` | Read raw models.dev catalog metadata |
| `meridian mars models refresh` | Force-refresh the models.dev cache |
| `meridian doctor` | Per-project diagnostics and orphan reconciliation (cheap, safe to run anywhere) |
| `meridian doctor --global` | Adds machine-wide committed-orphan and ad-hoc retention scans; must run from the root process (not inside a spawn) |
| `meridian doctor --prune` | Prune stale spawn artifacts and telemetry retention targets in the current project |
| `meridian doctor --prune --global` | Same as `--prune`, plus committed orphan roots and eligible ad-hoc runtime/context roots machine-wide |
| `meridian serve` | Start the MCP server |

`meridian doctor` scans for four categories of stale state and reports them as distinct warning codes:

| Warning code | What it means |
| ------------ | ------------- |
| `stale_spawn_artifacts` | Spawn artifact directories for completed spawns past the retention window |
| `stale_telemetry_segments` | Current-project telemetry segments that would be pruned by retention cleanup because they are expired or because total telemetry size exceeds the cap |
| `stale_orphan_project_dirs` | Project state directories with no matching live project (`--global` only) |
| `stale_adhoc_project_dirs` | Quiescent `adhoc.*` roots past their user-scope retention window (`--global` only); each row includes its reason and source path |

Local `--prune` removes stale spawn artifacts and telemetry segments selected by
current-project retention cleanup. Global pruning also removes committed orphan
roots and eligible ad-hoc roots with their matching `context/` directories.

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
| `meridian mars remove NAME` | Remove a dependency and prune its installed items |
| `meridian mars sync` | Compile packages into `.mars/`; materialize configured harness targets |
| `meridian mars link DIR` | Link compiled output into a harness tool directory |
| `meridian mars unlink DIR` | Remove a link target |
| `meridian mars list` | Show installed agents, skills, and bootstrap docs by name and description |
| `meridian mars upgrade` | Fetch latest versions and sync |
| `meridian mars migrate [--check]` | Rewrite pre-v1 authored content to schema v1; `--check` lists the changes and exits 1 |
| `meridian mars check [PATH]` | Validate a package's schema, structure, and plugin output |
| `meridian mars provide <provider>@<version>=<dir>` | Install a host's own skills under `.mars/skills/`; Meridian runs this itself on launch |
| `meridian mars repair` | Rebuild managed state from config and dependencies |
| `meridian mars doctor` | Check for drift and integrity issues |

Mars also has `adopt`, `override`, `rename`, `resolve`, `outdated`, `why`, `cache`,
`version`, and `build`; run `meridian mars <command> --help`, or read Mars's
[CLI reference](https://github.com/meridian-flow/mars-agents/blob/main/docs/cli/commands.md).
The `models` commands are under
[Configuration & Diagnostics](#configuration--diagnostics).

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

Meridian CLI processes default `MERIDIAN_MANAGED=1` in their environment, while preserving an explicit outer override. Mars uses this signal to suppress native agent emission to harness directories — agents are read by Meridian from `.mars/agents/`, not duplicated into `.claude/agents/` etc. `[settings.meridian.agent_copy] harnesses = ["claude"]` is the selective override when `.claude` is an effective managed target; it materializes qualifying Claude-native copies even under managed mode or `agent_emission = "never"`.

In Claude launches, native `Agent()` follows that same boundary: generic `Agent` is allowed only with Mars Claude `agent_copy` plus an effective `.claude` managed target. Without it, use `meridian spawn`. Claude built-ins (`Explore`, `Plan`, `General-purpose` / `general-purpose`) stay denied.

Headless Claude spawns are allowed by default. To prevent `meridian spawn -a <agent>` from launching them, set:

```toml
[spawn]
deny_headless_harnesses = ["claude"]
```

That blocks spawn routes after model/profile resolution selects Claude, while
leaving primary Claude launches available.

See [agent-profiles.md](agent-profiles.md) for the agent profile format including `model-policies`, `no-fallback`, and `mode`. Profile `fanout` and `fallback-order` are gone in schema v1 (dropped from pre-v1 content, an error in `schema = 1` packages); use `model-policies` list order + `no-fallback: true`, and [`[settings.meridian.fanout]`](configuration/mars.md#fan-out-agent-routing) for fan-out agents.

### Bundled Meridian skills

`meridian skills list` shows the operational skills shipped with Meridian and does
not create or update `.mars/`. `meridian skills NAME [RESOURCE-PATH]` reads one.
In a project with `mars.toml`, the named read first asks Mars to install the current
bundle under `.mars/skills/` (`mars provide`); it reads only names Mars reports as
provided and falls back to the wheel when Mars cannot provide them (a Mars older than
0.20 has no `mars provide`; a held lock; an interrupted sync, which `meridian mars sync`
recovers; a bundle Mars refuses) or their installed `SKILL.md` is missing or invalid. Unknown names are rejected before asking Mars to provide. In a
bare directory it reads the wheel without initializing the user Mars root.

The rendered skill body is the same from either source. Its `Files:` line names the
directory actually read, so a Mars-provided skill points at
`<project>/.mars/skills/<name>/` and its resource reads use that same copy.

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
