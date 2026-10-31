# Upgrade notes

## A continue keeps the spawn ID

`--continue pN`, `--continue cN` and the session browser now keep the spawn's ID: the
continued conversation is still `pN`, `MERIDIAN_SPAWN_ID` stays `pN`, and children started
after a continue record `pN` as their parent. `--fork` and `--from` still create a new spawn.
`spawn show pN`, `spawn wait pN` and `spawn report show pN` show the latest run; a wait that
started during an earlier run still returns that run's result. Continuing a spawn from another
spawn moves it under the caller, and a spawn cannot be continued from one of its own
descendants.

A spawn can be continued only while no run of it is live. A second terminal continuing the same
spawn is refused before anything is written: "pN is still running", or "pN is being continued
or repaired" while the first continue is still starting. Do not retry the second message; wait
for the first run to end.

A continue may change the model (`-m`: the run and `spawn show` use the new one), `--work` and
`--desc` (they replace the spawn's own; left out, they are kept). A primary continue still
refuses `--skills`, `--task-dir` and passthrough arguments. It accepts approval (`--yolo`), sandbox,
network, effort and autocompact, and now applies them to that run and keeps them as the spawn's
policy for later continues (`--sandbox` alone also resets the network, as a fresh launch does,
unless the network was set explicitly); earlier builds accepted them and ignored them.
A child continue (`spawn --continue`) refuses skills, approval, sandbox, network, effort,
autocompact, `--env`, `--task-dir` and passthrough arguments; it no longer refuses `--work`. Use
`--fork-fresh` to change what a continue refuses.

**What changed**

- Each run writes its files into its own directory under `spawns/pN/`. Use
  `meridian spawn report show pN` and `meridian spawn show pN` instead of building paths.
  The human output of `spawn show`, `spawn status`, `spawn report show`, `spawn report search`
  and `spawn wait` no longer prints a report path (the `spawn wait` table drops its `report`
  column). `report_path` stays in the JSON of `spawn show`, `spawn status`,
  `spawn report show` and `spawn report search` (the current run's report; a search hit's own
  file) but is deprecated and goes in the next minor release. `log_path` was never in the JSON.
  A run that wrote no report shows none; it never shows an earlier run's.
- An earlier run's report is still reachable: `spawn report search` looks in every run's
  report and names the spawn by ID, and `meridian session log pN` reads the conversation.
- Hooks: a continue fires `spawn.started` and later `spawn.finished` for the same spawn ID,
  not `spawn.created` for a new one. `spawn.finished` fires once per run, with a different
  `event_id` each time.
- Cost, tokens and duration are totals across every run of the spawn. A continue no longer
  restarts them. A spawn created before v0.18.0 counts the conversation's earlier usage again on
  its first continue: the harness reports the whole conversation's usage, and the older
  record kept no running total to subtract.
- Continuing a chat whose spawn chain began before the upgrade (`p5`, continued as `p6`,
  continued as `p9`) reopens the chat's newest spawn: `meridian --continue p5` prints
  "p5's conversation continues as p9". It refuses when that would change the newest spawn's
  parent or kind; continue it by its ID, or use `--fork`.
- `meridian --continue cN` on a chat that belongs to a child spawn is refused ("cN is child
  pN's conversation; `meridian --continue pN` makes it a primary"). Before v0.18.0 it started
  a new primary in that chat. `meridian --continue pN` makes the child a primary with the same
  ID.
- A spawn that ended without recording an outcome (killed, or its terminal closed) is
  recorded as `crashed` by the next continue, which also stops what it left running,
  including a managed primary's backend and TUI.
- `--dry-run` still reports without writing or stopping anything.

**Mixed versions.** Before running v0.18.0, update every worktree and install that shares this
project's runtime to v0.17.5 or later. Builds older than
v0.17.5 stop listing spawns and stop launching primaries once v0.18.0 has written a spawn.
v0.17.x builds refuse the spawns v0.18.0 created or continued, refuse prune and archive while
those exist, and may show a child's result a second time: an older build re-delivers a result
v0.18.0 already consumed, and never loses one. Spawns that an older build archives are not in
the v0.18.0 index until you run `meridian session index rebuild`.

**Pi.** Restart long-lived Pi sessions after upgrading. A Pi session started before the upgrade
can hide an earlier run's result after a continue, and marks it delivered without showing it.

## New spawns start running

A new spawn is `running` from the moment it exists. It no longer shows `queued` before its
runner starts or `finalizing` while it writes its outcome. `--status queued` and
`--status finalizing` (and `--view queued`) still parse, with a deprecation warning, and are
removed in the next minor release. Rows written by older builds keep those statuses.

## Removed hook names and keys

Hook config that the event-name migration deprecated is now rejected. `meridian hooks
check` and `hooks list` report each row as invalid, naming the replacement; runtime
dispatch drops invalid rows and keeps the valid ones.

| Rejected | Use instead |
| --- | --- |
| `event = "spawn.start"`, `"spawn.running"` | `spawn.started` |
| `event = "spawn.finalized"` | `spawn.finished` |
| `event = "work.start"`, `"work.started"` | `work.activated` |
| `event = "session.responded"` | `notify.your_turn` |
| `event = "session.cache_expiring"` | `notify.cache_expiring` |
| `failure_policy` | delete it; hook failures always continue |
| `when.status = ["skipped"]` | delete the value; it never matched |
| `when.status` on an event other than `spawn.finished`, or `when.agent` / `when.kind` on a work event | delete the condition, or move the row to an event that supplies it |

A row that used to run under an old name received that name in `event_name` and
`MERIDIAN_HOOK_EVENT`; after you rename it, it receives the new name, so update scripts
that compare against the old one. Rows named `spawn.start` or `work.start` never fired
before the window opened, and will start firing once renamed. `hooks list` no longer
shows a `deprecated` status. See [Hook event names](#hook-event-names) for the
original rename.

## Spawn file listing removed

`meridian spawn files` is removed. It never listed anything. Delete it from scripts,
including `meridian spawn files <id> | xargs git add`; stage changes with `git add`.

## Session browse `--include-archives` removed

`meridian session browse --include-archives` is removed; passing it is now an unknown
option error. It had no effect. Use `--status archived`, or Status: Archived in the
picker.

## Codex `network_access` native config removed

`harness-overrides.codex.sandbox_workspace_write.network_access` is a Meridian-owned key.
It used to override the network value Meridian projected, with a deprecation warning. A
fresh Codex launch whose profile sets it now fails with a config error naming the `network`
field. A continued session that an older build recorded with the key runs under its stored
`network`; the key is dropped. Replace the override with `network:` in
the profile (see [agent profiles](agent-profiles.md)).

## Per-chat work and checkout links

Each live session, primary or spawned, now reaches its work directory and source
checkout through two symlinks under the runtime root: `sessions/<chat>/work`
(`MERIDIAN_ACTIVE_WORK_DIR`) and `sessions/<chat>/task` (`MERIDIAN_TASK_DIR`). Both
follow the commands that move their target, and both survive a Claude `/clear` or
`/resume`. A spawned agent's links belong to its own chat, so its environment no
longer names the parent's directory; a primary names its links in the system prompt
and a spawn names them in its `# Meridian run` user turn.

A `--task-dir` given to a launch, a launch from a directory outside the project, and
a pin the launching execution passed on are now recorded on the execution itself, so a
later `work done`, `work rename`, `work switch` or `meridian task-dir` keeps that
checkout instead of resetting the link to the project root. `meridian task-dir clear`
returns the execution to its work item's checkout. A checkout an execution only
followed from its item or the project root is not pinned, so a child of such an
execution follows `work task-dir` and `work switch` too.

`work done` now moves an item's directory to the archive at once and writes no
session state; the item's own links follow the directory, and every session attached
to it follows them. A running spawn keeps writing through its link and its writes
land in the archive; a primary attached to the item keeps it. There is no deferred
move and no startup sweep, so `work done` no longer reports spawns that hold the
directory. `work rename` moves live executions onto the new name; a chat that ended
keeps its old name, so `--from` on it warns and creates nothing. Each item's links
are created by its first write or by the first session that attaches to it, and
`meridian doctor` republishes all of them.

A spawn launched by an older build and still running across the upgrade holds the old
concrete path, so it may recreate that directory after `work done` moves the item. A
later `work done` or `work reopen` refuses when both locations exist and names both;
reconcile them by hand. If a permission rule pins the real work-item or checkout path,
grant `sessions/<chat>/` instead, because harness path rules do not follow symlinks.

## Chat and session references

Every command that takes a reference now resolves it through one rule, so a chat ID, a
spawn ID and a raw harness session ID mean the same thing everywhere.

- Every spawn command (`show`, `status`, `children`, `wait`, `cancel`, `done`, `rearm`,
  `report`, `message`) takes a spawn ID, a chat ID or a raw harness session ID. A chat or
  raw ID names the run live in that chat now (a native `/resume` takeover, followed on a
  best-effort basis), else the spawn that ran it. A chat ID never means a child that
  chat launched. `cancel` on a primary still needs `--force`.
- A ref shaped `p<digits>` or `c<digits>` is always a Meridian spawn or chat. One that
  names nothing fails with a single not-found error (`Chat 'c99999' not found`) instead
  of falling through to the harness as an untracked session ID. Any other value is
  treated as a native session ID; only one with no Meridian record resumes untracked, and
  only through primary `--continue`/`--fork` and spawn `--fork`. Spawn `--continue`
  requires retained spawn metadata and refuses it.
- A raw harness session ID names the chat of the newest generation that has a native
  session key, else the newest generation. Resume and read share this rule. Before, it
  named the lowest-numbered chat, so `session log <id>` on a session recorded under two
  chats now reads the newer one. An ID recorded under two native stores (or
  two harnesses) is refused as ambiguous; pass the chat ID instead.
- `--continue`, `--fork`, `--fork-fresh`, `session log`/`export`/`search` and
  `session repair` accept `@latest`, `@last-failed` and `@last-completed`, as the
  spawn commands and `--from` do. `spawn message` refuses them. `session repair` also
  accepts a raw harness session ID.
- `--from cN` names chat `cN`'s own transcript, and `--from pN` the spawn's current chat,
  not its entry chat. A conversation that ran `/clear` is in a new chat, so a handoff
  passes the run's own spawn ID (`--from p123`; `meridian task-dir --json` prints it as
  `spawn_id`) to follow the current chat; a chat ID there reads the transcript
  from before the `/clear`.
- A message's reply command names the sender's current chat, so it still reaches a
  primary after a `/clear`. The old chat ID still names the run in `spawn show` and
  `cancel`, but `spawn message` refuses it.
- `--continue` on a chat a live run still holds fails within about two seconds, naming
  the holder (`Chat c1 is held by pid 4242; continue after it exits, or --fork c1`);
  continue after it exits, or `--fork` the chat. It used to wait on the lock. A
  continue's run becomes the chat's owner only when its session starts; until then
  `spawn show`, `wait`, `cancel` and `message` by chat ID name the previous run. See
  [Troubleshooting](troubleshooting.md#continue-refuses-the-chat-is-held).

`meridian [-C REPO] spawn message REF` accepts a chat ID and reaches the session
running in that chat now; `-C` addresses a session in another project. Chat and spawn
IDs count per project.

`--spawn-id` on `spawn message` and `spawn inject` is deprecated: it warns on stderr
and works until the next minor release. Pass the ref positionally instead.

## Mars 0.26.2

Meridian pins `mars-agents==0.26.2`. Run `meridian mars sync` after upgrading, and
`meridian mars --user sync` for a user root.

`mars sync` and `mars upgrade` print info-level diagnostics only with `--verbose`;
the default report keeps warnings and errors. When a sync adds or moves a Codex hook
handler, the `codex-hooks-need-trust` warning now says to open Codex and run `/hooks`
to review and trust it; Codex skips the handler until then.

## Mars 0.26.1

Meridian pins `mars-agents==0.26.1`. Run `meridian mars sync` after upgrading, and
`meridian mars --user sync` for a user root.

A plugin package no longer needs a per-target `opencode.ts` for a hook it writes as
`[[hooks.<Event>]]`: the OpenCode plugin tree renders the same `mars-<name>.hooks.ts`
that sync installs. Rebuild the plugin to pick it up.

## Mars 0.26.0

Meridian pins `mars-agents==0.26.1`, up from 0.24.1, so 0.25.0 and 0.26.0 arrive
together. Run `meridian mars sync` after upgrading, and `meridian mars --user sync`
for a user root.

### Package hooks are portable

A package writes a hook once in `hook.toml` as `[[hooks.<Event>]]` and Mars renders it
for Claude, Codex, OpenCode and Pi. The per-target native file is now named with
`native-hook`; the old `fragment` key still works for one release and warns
`hook-fragment-deprecated`. Sync reports what a target cannot render
(`hook-event-skipped`, `hook-answer-unsupported`), and Codex asks you to review new or
moved handlers in `/hooks` once. A plugin package that moves an `opencode.ts` fragment
into `[hooks]` lost it in the OpenCode plugin tree until 0.26.1; see
[Mars 0.26.1](#mars-0261).

### State written by Mars 0.23 and earlier is not migrated

Mars 0.25 removed the migration. It drops records it cannot interpret with a warning
and deletes nothing.

### Renamed diagnostics

`hook-fragment-*` is now `hook-native-*`, `plugin-hook-fragment-dropped` is
`plugin-native-hook-dropped`, and `hook.fragment-*` is `hook.native-hook-*`.

Mars's [0.25.0](https://github.com/haowjy/mars-agents/releases/tag/v0.25.0) and
[0.26.0](https://github.com/haowjy/mars-agents/releases/tag/v0.26.0) release notes
list the rest.

## Mars 0.24.1

Meridian pins `mars-agents==0.24.1`, up from 0.23.4. Run `meridian mars sync` after upgrading
(and `meridian mars --user sync` for a user root).

### MCP servers move to the files harnesses read

Mars now writes MCP servers where each harness reads them: Claude in the project's `.mcp.json`,
Codex in `.codex/config.toml`, and Pi in `.pi/mcp.json`. It no longer writes `.claude/.mcp.json`
or `.codex/codex_mcp.json`, which neither harness read; the next `meridian mars sync` removes
Mars's entries from them and deletes a file only if that leaves it empty. Meridian itself
passes no MCP flags, so a spawn sees the servers its harness finds from its working directory.

- Claude asks you to approve a project `.mcp.json` server the first time you run `claude` there.
- A spawn launched with `--task-dir` sees `.mcp.json` only if the checkout has it: commit the
  file, or run `meridian mars sync` in that checkout.
- A server that ships its own files (`${MARS_MCP_DIR}`) is written with an absolute path, so a
  committed `.mcp.json` shows per-machine diffs.
- Mars keeps a same-named MCP entry it did not write and warns; `meridian mars sync --force`
  adopts it. Remote servers (`url`, `headers`) are supported in `mcp.toml`.

### Selection and diagnostics

- A skill's own `skills:` frontmatter no longer pulls other skills; an agent's `skills` still do.
- `meridian mars outdated` fails when the dependency graph cannot resolve, and
  `meridian doctor` reports that as `updates_check_failed`. A dependency overridden by
  `mars.local.toml` shows `?` and is not counted as an available update.
- Diagnostic codes renamed: `mcp-invalid-name` is `mcp.name-invalid`,
  `plugin-codex-mcp-env-rename` is `mcp-lossy`; `import.mcp-not-converted` is gone.
- Hook config files (`.claude/settings.local.json`, `.codex/hooks.json`, `.cursor/hooks.json`)
  are written through symlinks, keep their permissions and key order, and end with a newline.

Mars's [0.24.0](https://github.com/haowjy/mars-agents/releases/tag/v0.24.0) and
[0.24.1](https://github.com/haowjy/mars-agents/releases/tag/v0.24.1) release notes list the rest.

## Mars 0.23.4

Meridian pins `mars-agents==0.23.4`, up from 0.20.2. Two profile keys moved
(`resident-rearm-budget` and `meridian-capabilities`), and three changes may alter what you
get without an edit: a user `config.toml` now applies keys it once ignored, narrowed
dependencies contribute MCP servers, and a direct dependency's default-visibility MCP servers
now register. The rest needs nothing from you.

**After upgrading, run `meridian mars sync`** (and `meridian mars --user sync` if you use a user
root). Launches do not sync, so until you do, installed profiles keep the old top-level keys,
which Meridian no longer reads, and their settings are ignored without a message.

### If your profiles set `resident-rearm-budget` or `meridian-capabilities`

Both keys moved under the `harness-overrides.meridian-cli` block, as
`harness-overrides.meridian-cli.resident-rearm-budget` and `harness-overrides.meridian-cli.capabilities`.
Meridian reads only the new location. `meridian-cli` is a reserved consumer namespace, like
`meridian-flow` (Meridian Flow's):
Mars keeps the block in the installed profile, raises no `unknown harness override` warning for
it, and never writes it to a harness file.

What happens to an old top-level key depends on where the profile comes from:

| Profile source | `meridian mars sync` | Launch |
|---|---|---|
| `.mars-src/` or `~/.meridian/mars/.mars-src/` (`--user`), no `[package] schema` | exit 0, warns ``authored `.mars-src` content uses the pre-v1 format (1 file to upgrade); run `mars migrate` `` | Works: sync moves the keys in the installed copy, so the budget and `spawn: false` apply. The file in `.mars-src/` keeps the old keys |
| Dependency without `schema` (a pre-v1 package) | exit 0, warns ``package `<name>` uses the pre-v1 format (N files to upgrade); run `mars migrate` in its repository`` | Works, as above |
| Dependency declaring `[package] schema = 1`, new | `meridian mars add` exits 2 and adds nothing: ``[field.removed] <name>: agents/<file>.md: `resident-rearm-budget` was removed; move it under `harness-overrides.meridian-cli.resident-rearm-budget` `` | The dependency is not installed |
| Dependency declaring `[package] schema = 1`, already installed under Mars 0.20.2 | The same `field.removed` error, exit 2, for the whole project: nothing updates, including other dependencies and `.mars-src/` items | The installed copy keeps the old keys, so its budget and `spawn: false` are lost silently until you fix it |

The warning in the first two rows repeats on every sync until the source is migrated. For the
last row, upgrade the dependency to a release that migrated, override it with a migrated copy,
or ask its maintainer to run `mars migrate`. A project that is itself a `schema = 1` package
(it has its own `agents/` directory) hits the same error; run `meridian mars migrate` in it.

`meridian mars migrate` moves both keys, values and order unchanged, in every row above
(`meridian mars migrate --check` lists the edit and exits 1; add `--user` for `~/.meridian/mars`).
On a schema 1 package it also adds `requires-mars = ">=0.20.0"` when none is declared; a
package that relies on these settings should declare `requires-mars = ">=0.22.0"` instead,
because Mars 0.20.2 only warns about the `meridian-cli` block and older Meridian never reads it. If a
profile already sets one of these keys under `harness-overrides.meridian-cli`, that value wins and the
top-level twin is dropped with a note; the keys it does not set move over. Package authors: Mars's
[migration guide](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/migrating-to-schema-v1.md#what-it-changes)
lists what the migration changes.

**Renamed namespace.** Mars 0.22.0 reports a
`harness-overrides.meridian` block (and `harness-overrides.flow`, now `meridian-flow`) as
``[field.removed] ... `harness-overrides.meridian` was renamed; use `harness-overrides.meridian-cli` ``.
Meridian reads only `meridian-cli`, so a profile still using `meridian` loses its
`resident-rearm-budget` and `capabilities` (including `spawn: false`) without a message
until the block is renamed. `meridian mars migrate` renames it in place, in schema 1 files too.
If the profile already has both blocks, they merge key by key: a key only the old block sets moves
over, and where both set a key the `meridian-cli` value wins and the old one is dropped with a note
(`field.restriction-lost` for a `capabilities` entry).

### MCP servers and hooks

- A dependency narrowed by an include filter (`agents`, `skills`, `only_skills`, `only_agents`)
  now contributes its MCP servers; before, sync silently dropped them. A newly visible server
  replaces a hand-written entry with the same name in that target's MCP config, so rename your
  entry if you want to keep it.
- `${MARS_MCP_DIR}` in an MCP server's `command` or `args` names that server's own `mcp/<name>/`
  files, which sync installs beside each target that writes MCP config.
- Unmanaged content at `<target>/hooks/<name>/` no longer fails sync. It warns, stays untouched,
  and withholds that target's hook entry; `meridian mars sync --force` adopts and overwrites it.

### User `config.toml` now applies (Mars 0.23.3)

The user `config.toml` (`$MARS_CONFIG_DIR/config.toml`, else the platform `mars/config.toml`)
now accepts the same `[settings]`, `[models]`, `[agents]` and `[skills]` sections as
`mars.local.toml`, so one file can replace per-project copies. Precedence, lowest to highest:
user `config.toml` < `mars.toml` < `mars.local.toml` < CLI. `[overrides]` is rejected there
with an error pointing to the project's `mars.local.toml`.

A key that Mars once ignored with a warning, such as `default_model` under `[settings]`, now
takes effect. If your user `config.toml` holds a stale value of that kind, it starts applying
after the upgrade; remove or fix it. Unknown keys no longer warn, as in `mars.local.toml`.

### Default-visibility MCP servers register (Mars 0.23.4)

A direct dependency's MCP servers with the default `local` visibility are now registered for
its consumer; before, only `exported` servers were. After `meridian mars sync`, every
default-visibility server from a direct dependency appears in your MCP config, and one with the
same name as a hand-written entry replaces it, so rename your entry to keep it. Transitive
dependencies still contribute only `exported` servers.

Smaller fixes: `mars check` reads dependency model aliases from `mars.lock` (0.23.2) and Claude
plugin import link warnings are accurate (0.23.1); `mars build plugin` registers agents in the
OpenCode plugin (0.23.0, package authors only).

Mars's docs also stopped describing `.agents/` as a default target: with nothing linked, sync
writes only to `.mars/`, and `meridian mars link DIR` adds a target. Meridian never depended on
`.agents/`. The MCP filter fix landed in Mars 0.20.5, and `${MARS_MCP_DIR}` and the hooks change
in 0.20.4. Mars's [0.20.3 through 0.23.4](https://github.com/meridian-flow/mars-agents/blob/main/CHANGELOG.md)
changelog lists the rest, including plugin-build fixes that affect only package authors.

## Mars 0.20.0 (schema v1)

Meridian 0.15.0 pinned `mars-agents==0.20.2`, up from 0.18.4. Mars 0.20 reads and writes schema v1,
and it carries 0.19.0's changes too, which the old pin skipped. Mars documents the format
([schema v1](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/schema-v1.md),
[migrating a package](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/migrating-to-schema-v1.md))
and each change in its [0.20.2](https://github.com/meridian-flow/mars-agents/releases/tag/v0.20.2),
[0.20.1](https://github.com/meridian-flow/mars-agents/releases/tag/v0.20.1), [0.20.0](https://github.com/meridian-flow/mars-agents/releases/tag/v0.20.0)
and [0.19.0](https://github.com/meridian-flow/mars-agents/releases/tag/v0.19.0) release notes.
This section lists what a Meridian user does about them, grouped by what you are editing.

### Right after installing

Run `meridian mars sync` once. Sync rewrites installed `.mars/` content as schema v1.

- Until it runs, launches keep working: Mars upgrades installed pre-v1 profiles and skills in
  memory the way sync would (`approval: yolo` reads as `never`; a skill's `type` as
  `metadata.type`), and drops what schema v1 removed, such as a profile's `autocompact`.
- A `meridian mars sync --frozen` (CI, scripts) fails with `frozen violation: lock file would
  change but --frozen is set` until a plain sync has run, because the rewrite changes the
  checksums of packages authored before schema v1. Run `meridian mars sync` once and commit
  `mars.lock`; `--frozen` passes after that.
- Sync warns ``package `<name>` uses the pre-v1 format (N files to upgrade); run `mars migrate`
  in its repository`` on every run for each such dependency. It installs and works; only its
  maintainer clears the warning.

### If you write `mars.toml` overlays

Mars checks `[agents.<name>]` overlays when `mars.toml` loads. A bad value fails every
`meridian mars` command and every launch, not only sync.
Mars's [overlay reference](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/mars-toml.md#agentsname-overlay)
lists the accepted keys.

| You have | Mars 0.20.0 does | Change it to |
|---|---|---|
| `[agents.X] approval = "yolo"` | load error: `` `approval` has invalid value `yolo`; allowed: default, auto, confirm, never `` | `approval = "never"` |
| `autocompact`, `autocompact_pct` or `harness` in a `[[agents.X.model-policies]]` `override` | load error: `` model policy override `autocompact` has invalid value `` or `` unknown model policy override `harness` `` | Delete it. An override sets only `effort`, `sandbox`, `approval` and `network`. Set `harness` on the profile or the `[models.<alias>]` entry, and compaction as below |
| `autocompact` or `autocompact_pct` directly in `[agents.X]`, or a `[skills.X.tools]` table | warning `` is no longer read and is ignored ``; nothing applies | Delete it |
| An invalid `effort`, `sandbox`, `approval` or `network` in `[agents.X]` | load error | Fix the value |

Meridian's own spellings keep working: `--approval yolo`, `--yolo` and `MERIDIAN_APPROVAL=yolo`
run as `never`. Only `mars.toml` and profile files lost `yolo`.

Mars now takes compaction only from the model alias. Set `autocompact` or `autocompact_pct` under
`[models.<alias>]` in `mars.toml` (see Mars's
[`[models]` reference](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/mars-toml.md#models)),
so it applies to every launch that uses the alias, or set `primary.autocompact` /
`primary.autocompact_pct` in `meridian.toml` for the primary session only. An alias that sets
its own value wins over `primary.autocompact`.

### If you write profiles or skills

Pre-v1 content in `.mars-src/` is upgraded in memory on every sync, with one warning:
``authored `.mars-src` content uses the pre-v1 format (N files to upgrade); run `mars migrate` ``.
`meridian mars migrate` rewrites the files once and names each key it deletes
(`meridian mars migrate --check` lists the changes and exits 1 if there are any). Run it from a
project, or add `--user` for the profiles in `~/.meridian/mars`; outside a project, `migrate`
without `--user` is refused, and `--check` still runs.
In a project whose `mars.toml` declares `[package]`, `migrate` rewrites the package's own
`agents/` and `skills/` and leaves `.mars-src/` alone; edit those files by hand.

- **Renamed or converted, behaviour kept:** `disallowed-tools` becomes `deny` entries in
  `tools`; a `tools` list becomes a map; `model_invocable` and `network_allow` take their
  hyphenated spellings; `approval: yolo` becomes `never`; a skill's `type` becomes
  `metadata.type` and `when_to_use` is appended to `description`.
- **Deleted, behaviour lost:** a profile's `autocompact` and `autocompact_pct`; `autocompact`,
  `autocompact_pct` and `harness` in a `model-policies` override; `fanout`, `models` and
  `mcp-tools`; a skill's `tools`, `allowed-tools` and `disallowed-tools`. Sync alone drops them
  without naming them, so run `migrate` and restore what you still want: compaction on
  `[models.<alias>]`, fan-out candidates in
  [`[settings.meridian.fanout]`](configuration/mars.md#fan-out-agent-routing), MCP grants as
  `mcp(server): allow` entries in `tools`.
- **Moved:** `resident-rearm-budget` and `meridian-capabilities` now live under
  `harness-overrides.meridian-cli`. See [Mars 0.23.4](#mars-0234).

A package you publish with `[package] schema = 1` is read strictly: a removed key fails sync with
exit 2 and changes nothing. `mars migrate` also adds `requires-mars = ">=0.20.0"` to a package
that declares no requirement, and `meridian mars check` warns when a `schema = 1` package has no
requirement or one that allows an older Mars. Package maintainers: see Mars's
[migration guide](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/migrating-to-schema-v1.md).

### If you depend on packages

- A marketplace dependency with several plugins and no `plugin` key now fails sync with
  `` `<name>` is a marketplace with these plugins ``; before, every plugin installed. Name each
  plugin you want: `meridian mars add <marketplace> --plugin <name>`, or `plugin = "<name>"` on
  the dependency entry.
- A file symlink inside a skill or profile that points outside its package (in `.mars-src/` or a
  path dependency) now fails sync with ``authored package entry escapes package root``; before,
  Mars copied the target. Copy the file into the package. A symlink that stays inside the package
  works.
- Mars 0.20.0 and 0.20.1 refused `mars check <path> --root <dir>`, which is how
  `meridian mars check <path>` reaches Mars, and failed to sync a dependency without a
  `mars.toml` whose `skills/` directory holds a stray file such as `__init__.py` or `README.md`
  (`Not a directory`). Mars 0.20.2 and later fix both; they behave as they did on
  0.18.4 and need nothing from you.
- Mars 0.19.0 orders hooks by shortest dependency depth, which changes the order only when a
  direct dependency is also reached transitively; Codex may ask you to re-trust moved hooks once
  (`/hooks`). It also moves Mars-owned `mcpServers` entries in an `.opencode` target to OpenCode's
  native `mcp` key on the next sync. Mars's 0.19.0 release notes describe both.

### Policy that changes at launch

- **`effort: max` now runs at `max`.** Mars 0.18.4 capped `max` to `xhigh` before Meridian saw
  it, so a profile or `mars.toml` overlay that said `max` ran at `xhigh` on every harness. Mars
  0.20 passes `max` through: Claude receives `--effort max`, Codex `model_reasoning_effort="max"`
  and Pi `--thinking max`. Those launches now run higher, which costs more and runs slower. Set
  `xhigh` to keep the old behaviour.
- Mars gained `effort: none` (reasoning off). Meridian lowers it per harness: Claude receives no
  `--effort` and keeps thinking, because current Claude models cannot turn thinking off (the
  dry-run says so); Codex receives
  `model_reasoning_effort="none"`; Pi receives `--thinking off`. OpenCode applies no effort.
  `--dry-run` reports a launch that omits the flag. See
  [Effort by harness](agent-profiles.md#effort-by-harness).
- **Per-model rules in a profile now apply.** A matching `model-policies` rule's `override`
  beats the profile's top-level value for `effort`, `sandbox`, `approval` and `network`. Under
  Mars 0.19 and earlier the top-level value won, so a profile that set `effort` never ran its
  own per-model `effort` rule; that was a bug, and schema v1 fixes it. A rule in a `mars.toml`
  overlay (`[[agents.<name>.model-policies]]`) already beat the top-level value and still does.
  A profile with `effort: max` and a `deepseek` rule at `high` ran at `xhigh` under Mars 0.18.4
  and runs at `high` now, and one with a `sonnet` rule at `medium` runs at `medium`. A profile
  with `approval: never` and a matching rule at `confirm` now launches at `confirm`, which makes
  a headless spawn refuse its tool calls. Delete the rule's override to keep the top-level
  value.
- **Packaged profiles change their effort.** Their per-model rules were written for the rule
  winning, and now they do. Only `effort` moves: no packaged rule sets `approval`, `sandbox` or
  `network`. Checked against `meridian-dev-workflow` 0.15.16, `meridian-base` 0.12.1 and
  `creative-writing-skills` 0.5.19:

  | Profile | Ran at | Runs at now |
  |---|---|---|
  | `coder` | `xhigh` (top-level `max`) | `high` on deepseek and luna; `medium` on sonnet, opus and sol |
  | `tech-lead` | `medium` | `xhigh` on grok; `high` on deepseek and astra |
  | `reviewer` | `medium` | `high` on grok, deepseek and luna |
  | `explorer` | `medium` | `low` on deepseek and sonnet |
  | `continuity-checker`, `critic`, `editor`, `style-creator` | `high` | `low` on deepseek |

  The list is not complete; `meridian spawn -a <profile> --model <alias> --dry-run` shows the
  effort a launch resolves to. Other packages with a top-level `effort` and a matching rule change
  the same way.

### Rolling back

Rollback works. Install the previous Meridian, which pins Mars 0.18.4, and run
`meridian mars sync` once, then commit the `mars.lock` it writes. With a dependency authored
before schema v1, a `--frozen` sync fails first, the same way it does after the upgrade. Mars
0.18.4 reads the schema v1 content this release leaves behind: packages that declare
`schema = 1`, and `.mars-src/` files you ran `migrate` on. Checked with the previous Meridian
release and Mars 0.18.4 against projects last synced by this build.

- A skill whose type lives in `metadata.type` reports the type `reference` again, because
  Mars 0.18.4 reads only a top-level `type`.
- Keys `meridian mars migrate` deleted stay deleted; restore them from version control.
- Per-model `effort` rules stop applying again: a profile's top-level `effort` wins over its
  `model-policies`, as it did under Mars 0.18.4.
- `.mars/provided.lock` and the `.mars/skills/*__meridian` folders stay on disk and the old
  build ignores them. `.mars/` is rebuildable, so deleting it and syncing also works.

## Meridian-owned operational skills

Guidance whose subject is Meridian itself now ships with the CLI instead of a
prompt package. Every agent sees short triggers for `sessions__meridian` and
`md-validation__meridian` (the `kg` and `mermaid` validators' mechanics); profiles
declaring `mode: primary` also see `escalation__meridian`, `handoff__meridian`,
`messaging__meridian`, `notify__meridian`, `prompt-authoring__meridian`, and
`work__meridian`. The `name__meridian` suffix follows
Mars's `name__source` collision spelling, and every bundled skill uses it so a
skill does not change names between profiles. The agent reads applicable guidance
on demand with `meridian skills NAME`.
`meridian skills NAME RESOURCE-PATH` reads a referenced resource, and
`meridian skills list` prints the catalog (`--json` returns name/description
objects). These binary-owned skills include the concrete Meridian commands and
flags that version with the CLI; the short injected rows remain pointers.

Remove package copies of these Meridian-specific skills from agent profiles as
their package release makes them obsolete. Package skills remain unchanged:
`load`, `available`, and `--skills` still use the harness's native skill
mechanism. The injected capability rows continue to name the actual Meridian
commands; the bundled skills carry the situational judgment rather than
duplicating it in every system prompt.

The capability rows and resolved directory facts are rendered together at bind
time. Only spawn-capable profiles receive delegation rows and the
`meridian work path` platform grant used to prepare prompt files.

## Shell network policy defaults

Agent profiles can set `network: allow`, `network: deny`, or `network: allowlist` to control
connections made by shell commands, including public internet access, host services, and
local sockets. Built-in web tools remain controlled separately by `tools: web`.

`allowlist` permits the `network-allow` host set, less the `network-deny` set. `allow` may
also carry denies. Exact hosts, IP addresses, `*.x` (subdomains only), and `**.x` (apex plus
subdomains) are accepted. Plain host lists add across config/profile/overlay/CLI layers,
while an allowed `{replace: [...]}` clears lower entries; denies win. An empty allowlist
fails closed to `deny`. See Mars's [network profile reference](https://github.com/meridian-flow/mars-agents/blob/main/docs/config/agent-profiles.md#network)
for full merge and replacement rules.

When `network` is omitted, it follows the profile sandbox:

- `read-only` and `workspace-write` project deny, exactly as if the profile authored
  `network: deny`;
- `danger-full-access` stays online; and
- an omitted or `default` sandbox stays at the harness's native behavior.

Sandboxed profiles therefore stay offline unless they opt in. To give one shell network,
set:

```yaml
sandbox: workspace-write
network: allowlist
network-allow: [github.com, "*.github.com"]
network-deny: [uploads.github.com]
```

For one launch, `meridian`, `meridian spawn`, and `meridian bootstrap` accept
`--network allow|deny|allowlist` plus repeatable `--network-allow HOST` and
`--network-deny HOST`. The host flags add to the lower-layer sets. `meridian spawn
--continue` rejects network and sandbox overrides. Primary `meridian --continue` replays
the stored network and sandbox policy, ignoring new values for either; a fork recomposes
policy with the new flags.

`[primary] sandbox = "read-only"` or `"workspace-write"` in `meridian.toml` now also takes
the primary shell offline when the network mode is unset. Set the network in Mars config or
pass `--network allow` (or an allowlist) to opt in.

Claude now projects `sandbox.network.strictAllowlist: true` for an omitted network under a
constrained sandbox. In particular, sandboxed launches using `approval: never` (or the old
`yolo` spelling) that reached the network when Claude received no network keys are now
blocked. Codex projects `sandbox_workspace_write.network_access=false`; this overrides a
user `config.toml` value of `true` unless the profile explicitly sets `network: allow`.
Continued sessions with an older launch-policy snapshot and no network value follow the same
sandbox-derived default.

Codex `workspace-write` uses its network proxy for finite allowlists and `allow` with
denies. Local services remain reachable, so the dry-run report marks those policies
partial. Codex `read-only` cannot enable network. Its dry-run report calls out an explicit
`network: allow` or non-empty allowlist; leaving the field unset reports and keeps native
deny. No harness can
enforce `network: deny`
under `danger-full-access`, and OpenCode, Pi, and Cursor cannot enforce it in any mode.

Claude translates `**.x` to `x` plus `*.x`. A finite allowlist keeps ordinary user
local-binding and Unix-socket settings disabled unless it contains `localhost` or
`127.0.0.1`; either name enables Claude's coarse local-binding and all-Unix-sockets
switches.

Meridian does not try to reproduce either harness's config precedence. It warns about
settings that can widen access beyond the policy Meridian projected:

- in the user `settings.json` under the root selected by the child
  `CLAUDE_CONFIG_DIR`, a non-empty `sandbox.network.allowedDomains` or the presence of
  `httpProxyPort` or `socksProxyPort` (including a JSON number written as a float);
- in the child-selected `$CODEX_HOME/config.toml`, any `allow` entry under
  `features.network_proxy.domains`;
- in `.codex/config.toml` in the child's working directory or any ancestor, the same Codex
  allow entry, regardless of project trust settings.

Dry runs and launches name every detected file and warn that it can widen access. These
warnings do not change the permission report, caller boundary, or sandboxed-child
guardrail: those compare only Meridian's projected policy. The harness still reads its own
configuration, so actual access can be wider. Narrowing settings such as Claude
`deniedDomains` and Codex domain `deny` entries are not modelled. A boolean Codex
`features.network_proxy` value declares no domains and does not warn. If a selected file is
unreadable or malformed, Meridian names it in a warning; the native harness can still
refuse the file itself.

**Before installing**

Upgrade Meridian so its installed `mars-agents` dependency is 0.18.0 or later:

```bash
uv tool upgrade meridian-cli
meridian mars --version
```

A Mars bundle without `provenance.network_source` predates the portable field. Meridian
refuses the launch, names the Mars executable it used, and asks you to upgrade Meridian
rather than silently leaving the harness at its native network default. Meridian prefers
its bundled Mars over a separate executable on `PATH`.

Replace Codex `harness-overrides.codex.sandbox_workspace_write.network_access` with the
portable profile field. A well-formed boolean native value was deprecated in this release.
Launch now fails with a config error; see
[Codex `network_access` native config removed](#codex-network_access-native-config-removed).

## Child guardrail follows the harness

The child-spawn guardrail compares the sandbox and network each harness actually enforces,
on both the caller's and the child's side.

| Harness | `read-only` runs as | `workspace-write` runs as | Shell network |
|---|---|---|---|
| Claude, Codex | `read-only` | `workspace-write` | enforced |
| Cursor | `workspace-write` (its sandbox still allows workspace writes) | `danger-full-access` | open |
| OpenCode, Pi | `danger-full-access` | `danger-full-access` | open |

- A caller whose harness runs it as `danger-full-access` (OpenCode, Pi, or a Cursor
  `workspace-write` profile) has no floor: it can launch any child.
- A child is compared by what its harness runs. A sandboxed caller is refused a `read-only`
  explorer routed to Pi, with a message naming the harness. This previously passed,
  although nothing confined the child.

If a sandboxed caller's subagent routes to one of those harnesses by default, pass
`--harness claude`/`--harness codex` or a model that routes there, or change the child
profile's model.

## Sandboxed child delegation

A standalone `meridian spawn ...` can run outside a sandbox that Meridian projected for
the caller. Callers launched with an explicit `sandbox: read-only` or
`sandbox: workspace-write` on Claude or Codex therefore have a child-spawn CLI guardrail
(see [child guardrail follows the harness](#child-guardrail-follows-the-harness)). Child create,
continue, and fork operations now refuse when:

- the caller profile has no non-empty `subagents:` list (or explicitly disables Meridian
  spawn capability);
- the child's resolved sandbox is weaker than the caller's;
- the child's Meridian-projected network is not contained by the caller's. For host policies,
  every child allow must remain in the caller's allow set and every caller deny must remain
  denied by the child;
- native harness arguments follow `--`, because they could override the resolved policy.

These calls worked without this boundary before. Put the intended children in the caller
profile's `subagents:` list and move required harness-native arguments into the profile.
Approval is not compared independently. A current `read-only` or `workspace-write` caller
always records its projected network, so an omitted profile value records deny and accepts an
omitted constrained child. A boundary record written before the network field existed still
creates no network floor, preventing a mid-session upgrade from refusing nested spawns.
Claude and Codex widening settings do not raise that floor: they produce attributed
warnings, while the guardrail continues to compare Meridian's projected policies. This
includes Codex `.codex/config.toml` in any ancestor without modelling trust. Narrowing
harness settings are ignored.
Child write roots and `--env` are not part of the guardrail. Profiles using
`sandbox: default` or `danger-full-access`, and top-level shells not launched by Meridian,
carry no floor.

This is a guardrail against widening a child through Meridian's CLI, not confinement
against an agent that can rewrite launch inputs. In particular, a sandbox plus spawn
capability does not confine an adversarial agent. See the residual inputs and scope in the
[platform-rules reference](agent-profiles.md#platform-rules) and
[#747](https://github.com/haowjy/meridian-cli/issues/747).

If Claude's own settings enable its sandbox while a profile leaves `sandbox` unset,
Meridian does not run child creation outside that user-configured sandbox. The PID-namespace
guard refuses the nested launch instead. Set `sandbox:` explicitly in the profile when it
must launch children.

## Agent-profile permissions

Agent permission profiles are now binding on more harnesses and use typed rules from Mars.
Audit every profile that sets `tools`, `sandbox`, or `approval`; use
`meridian spawn -a NAME --dry-run` to see the exact projection and its gaps.

**Before installing**

Upgrade Meridian so its installed `mars-agents` dependency is 0.17.0 or later:

```bash
uv tool upgrade meridian-cli
meridian mars --version
```

Meridian no longer reads the launch bundle's flat `allowed`/`disallowed` lists. An older
Mars stops every launch with `Install mars >= 0.17.0`. `meridian mars upgrade` updates
package sources; it does not upgrade the Mars executable.

**List and scoped-allow profiles**

- The list form `tools: [read, grep]` is deprecated; Mars warns. It still only pre-approves
  the listed tools and leaves everything else at the harness default. Write a map instead,
  and add `'*': deny` when the list meant "only these tools":

  ```yaml
  tools:
    '*': deny
    read: allow
    grep: allow
  ```

- A capability with only scoped allows is an allowlist for that capability. With only
  `'bash(rg *)': allow`, unmatched Bash calls are denied, whatever `approval` says. Add
  `bash: allow` if the unmatched calls should remain available.
- `approval: never` means "do not prompt"; it does not override an allowlist.

Claude, OpenCode, and Pi enforce whole-policy and scoped-only allowlists, subject to the
gaps in the [permissions table](agent-profiles.md#permissions-by-harness). Codex and Cursor
report these allowlists but do not enforce them.

**Harness behavior to review**

- **Claude:** scoped rules and `ask` now use native settings instead of sending every
  non-deny as an allowed tool. Allowlists refuse unmatched state-changing calls even with
  `approval: never`. Claude also applies profile sandbox modes, although its sandbox confines
  Bash subprocesses rather than Edit/Write. Review profiles that relied on unmatched calls
  running, and set `[harness.claude] allow_builtin_agents = false` if they must remain disabled;
  `true` now takes effect. A spawn whose profile sets `'*': allow` now has the task-list tools
  (`TaskCreate`…`TodoWrite`) denied too; only an explicit `task: allow` (or `ask`) keeps them.
- **OpenCode:** profile `tools` now bind the selected `meridian-<profile>` config agent in
  primary sessions as well as spawns, on V1 and V2. Review profiles whose primary was
  intentionally unrestricted. A primary `ask` prompts the human; a spawned `ask` becomes
  an immediate native denial.
- **Pi:** mapped plain, scoped, allowlist, and `ask` rules now bind both RPC spawns and
  primary TUI sessions. Review profiles that previously treated Pi rules as descriptive.
  Remove Pi tool-selection passthrough flags from launches with a profile policy; Meridian
  now rejects them so they cannot override the policy. Under a Bash allowlist the extension
  refuses shell syntax it cannot split into simple commands (`$VAR`, redirects such as
  `2>/dev/null`, loops), so an allowlisted agent must run plain commands; beside
  `bash: allow`, scoped Bash denies are best effort and such a command runs unchecked.
- **Codex:** exact unscoped `bash`, `web`, `web_search`, and `image` allow/deny rules now use
  native per-launch switches. Scoped rules, `ask`, allowlists, and other capabilities remain
  report-only.

**Claude compaction settings**

Audit Claude profiles that set `autocompact`. Its token count was written to a percentage
variable and had no effect. It selects Claude's `--autocompact` window when the value is from
100,000 through 1,000,000 tokens; other values are reported and omitted. `autocompact_pct`
applies separately as a percentage.

**Codex harness overrides**

Treat installed `harness-overrides.codex` blocks as trusted package content. Codex applies
them as native configuration, including provider and MCP server commands; installed blocks
that were inert take effect after upgrading.

**Set Codex and Cursor sandboxes explicitly**

Profiles that set `tools` but leave `sandbox` unset no longer receive a sandbox inferred from
their tool rules. In 0.11.8, Codex used this inference:

- `danger-full-access` when `external_directory` resolved to `allow` or `ask`;
- `danger-full-access` when `*` resolved to `allow` or `ask` and both `edit` and `bash`
  also resolved to `allow` or `ask`;
- `workspace-write` when either `edit` or `bash` resolved to `allow` or `ask`;
- `read-only` for every other profile with `tools`.

Mars supplied harness-native names then, so most Codex profiles missed the semantic checks
and took the `read-only` branch. Meridian now omits the sandbox when it is unset and Codex
uses its own default. Set `sandbox` explicitly to preserve the mode you intended.

Cursor previously received the same shared inference: inferred `read-only` became
`--sandbox enabled`, while inferred `workspace-write` or `danger-full-access` became
`--sandbox disabled`. Meridian now omits `--sandbox` when the profile leaves it unset, so
Cursor uses its own default. Set `sandbox: read-only` to preserve the former restriction,
or set the intended permissive mode explicitly.

**Move OpenCode permission configuration out of `OPENCODE_PERMISSION`**

Meridian no longer sets `OPENCODE_PERMISSION`. It puts the compiled rules on its reserved
`meridian-<profile>` agent inside `OPENCODE_CONFIG_CONTENT` for both OpenCode versions.
Remove wrapper logic that reads or sets `OPENCODE_PERMISSION`; express the policy in the
profile's `tools` field. Do not reuse the reserved `meridian-<profile>` name for a user
agent, because OpenCode can merge non-colliding user fields into Meridian's entry.

**Use canonical MCP spellings**

Write MCP rules as `mcp(server)`, `mcp(server/tool)`, `mcp(server/*)`, `mcp(*/tool)`, or
`mcp(*/*)`. A bare `mcp` or `MCP` is now a validation error; replace it with an explicit
server/tool reference. `*` is the only wildcard, and server/tool case is preserved. Do not
write harness wire names in Mars-native profiles.

Mars now carries the native spelling separately: for example, `mcp(github/create_issue)`
reaches Claude as `mcp__github__create_issue` and OpenCode as `github_create_issue` instead
of being re-cased by Meridian. Review MCP rules that previously relied on a misspelled rule
doing nothing. Codex and Pi cannot project individual MCP references and report the gap;
Cursor does not enforce profile tool rules.

**Approval requests in spawns**

A runtime request nobody can answer is refused immediately and never sent to the parent
agent. Codex and observable OpenCode refusals are written to
`permission_requests.jsonl`, now with a one-line `summary`, and `meridian spawn show` lists
them. OpenCode controls the reason shown to the model and can remove a denied tool from its
catalog without emitting an attempted-call event, so not every native denial has a journal
row. Claude and Pi journal the native denials they report.

Spawns with tool rules keep narrow grants for `meridian spawn done` and
`meridian spawn rearm`, so an allowlisted child can answer Meridian's completion
confirmation without gaining a general shell. A stricter overlapping Claude or OpenCode
profile rule can still block a spelling; the dry-run report marks that grant partial.

**Launches you continue or fork**

A spawn's launch policy snapshot written by an older build holds the old tools map. This
build reads it as native-only rules, emits the recorded names as they were written through
this build's channels (a Claude scoped deny now lands in `--settings` rather than
`--disallowedTools`), and marks those lines `[legacy snapshot]` in `--dry-run`. It cannot
interpret them by capability, so Codex keeps only the `web_search` switch from them.

Such a snapshot also lacks the native names of the platform sub-agent and task-list denies.
A replay takes them from a fresh Mars bundle for the same harness, so the denies hold as on
a new launch. Only if Mars cannot build that bundle are the platform denies unnamed:
`--dry-run` reports both as `unsupported`. On Claude a profile rule for the same capability,
whose own tool names the snapshot kept, is still turned into a deny. The continued launch writes a new snapshot that
carries the names. Probed with a snapshot written by 0.11.8: a Claude `--continue` denies
`Agent`, `SendMessage`, `ListAgents`, and the `Task*`/`TodoWrite` tools, and a Codex
`--continue` kept web search.

## Spawn results consumed by a parent moved to `spawns/<parent>/`

`observed-spawns.json` records which child results a parent has already seen and
which children a running `spawn wait` holds. It now lives in the parent's own spawn
directory, `spawns/<parent>/observed-spawns.json`, and goes away when that spawn is
pruned or archived. It used to sit under `pi-bash/<parent>/`, a name left over from
Pi, although `spawn wait`, `spawn ack` and `spawn collect` use it for every harness.

Since v0.18.0 this file is only read. A continue keeps a child's ID, so a result is
consumed per run, not per child: `spawn wait`, `spawn ack` and `spawn collect` record
`(child, run)` in `spawns/<parent>/observed-launches.json`, and the Pi receipts moved to
`pi-bash/<parent>/launch-receipts.json` and `cleared-launches.json`. An entry that
names only a child in the old files reads as that child's first run, which is exact:
older builds could not read a continued spawn, so they only ever consumed its first run.
See [a continue keeps the spawn ID](#a-continue-keeps-the-spawn-id).

**What changed**

- Every reader still reads `spawns/<parent>/observed-spawns.json` and honors its consumed
  IDs and live waits, so results already consumed stay consumed and a `spawn wait` still
  running from the old build keeps holding its children. A damaged file counts as empty.
- The `pi-bash/<parent>/observed-spawns.json` mirror is neither read nor written any more.
  Nothing deletes either old file. A build older than v0.18.0 still writes them, and does not
  see what v0.18.0 recorded, so it may show a result a second time.

**Before installing**

Nothing is required. Finish or cancel `spawn wait` commands you can, but one that
keeps running through the upgrade is still honored.

**Rolling back**

An older build reads only the old location. Pi parents' records are mirrored there,
so nothing changes for them. For other parents (Claude primaries), results consumed
after the upgrade look unconsumed to the older build, and `spawn collect` can deliver
them again once. No data is lost or corrupted; older builds ignore the extra file in
the spawn directory.

## Session browse redesign (history index schema 12)

`meridian session browse` was rewritten (see the changelog). The on-disk changes
behind it are additive or rebuildable; nothing you wrote by hand is migrated.

**What changed, and why**

- Session start records gain defaulted fields: `tmux_target`, `context_from_chat_id`
  and `context_from_history_id`. A fork or `--from` chat now records the context it
  actually used, and that lineage carries across continuation, so the picker can
  draw the lineage tree.
- `model_observation` rows now hold the raw native model token, and a primary
  session also writes one when it stops, so browse can show the last-used model
  for chats never continued. Continue maps the raw token to a routable model when
  it launches.
- The history index moves from schema 10 to 12 (`history-v12-sessions-v1.sqlite3`):
  lineage edges and model observations are indexed, and the `previews` table is gone.
- Idle state files in format v1 are read as v2 (last turn and TTL kept) and
  rewritten as v2 on the next write.
- A `spawn --bg` request (`bg-worker-request.json`) keeps the raw prompt and no
  longer carries `prompt_is_composed` or `context_from`; the worker composes the
  prompt once.

**Before installing**

Nothing is required. Running `--bg` spawns can finish on the old worker.

**After installing**

Nothing is required. The first indexed read (any `session browse`, `session log`
or `session index` command) rebuilds the v12 index from the authoritative files and
may take a little longer on a large project. The old `history-v10-sessions-v1.sqlite3`
stays on disk unused; delete it once you no longer need to roll back.

**Data no longer kept**

The preview pane and its cache (the index `previews` table, and the preview counts
in `session index status` and rebuild) are removed. Previews were derived from the
transcripts, so nothing is lost; `Space` opens the full transcript.

**Scripts**

`session browse --plain --format json` rows drop `live`, `historical`,
`native_status` and `history_id`. `reentry.kind` (`resume`, `fork` or `blocked`)
replaces `live` as what Enter does; an archived row's `chat_id` is its history id.
The plain table columns are now STALE, TMUX, AGENT, IDLE (time since last turn), WORK.

**Rolling back**

Rolling back to 0.11.4 works. Probed on a copy of a real project runtime (9 chats)
written by this build, then read by 0.11.4:

- Older builds ignore the new start-event fields; a chat with `tmux_target` and
  `context_from_*` listed and logged normally.
- A raw-token `model_observation` row was ignored by the old `session browse`.
- 0.11.4 builds its own `history-v10-sessions-v1.sqlite3` next to the v12 file
  (`session index status` reports it current) and lists every chat. The picker
  loses the new columns, the model and the lineage tree.
- Idle files rewritten as v2 are the format 0.11.4 writes.
- A `--bg` request written by this build and picked up by a 0.11.4 worker defaults
  to a composed prompt and composes it once from the raw text, as before the fix. I
  did not run a live `--from` spawn through that mixed case, so a `--from` context may be missing on
  a spawn queued before the rollback.

## Completion signal reporting

`meridian spawn done` and `meridian spawn rearm` now refuse, with exit code 1,
when nothing will read the signal: a primary session, a spawn that is no longer
running, a spawn with no completion hold (Cursor and other plain
plans), `rearm` on Pi, and `rearm` after the rearm budget is spent. Scripts that
relied on these commands always succeeding will now see the failure.

The runner now records the signals its spawn reads in
`spawns/<id>/accepted-signals.json` once it has chosen how to wait for
completion. A spawn started by an older runner has no such file; the commands
still write the signal and say that delivery is unconfirmed.

Nothing needs doing before or after upgrading. Spawn rows are unchanged and
older builds ignore the new file, so rolling back works; an older CLI's
`spawn done` / `rearm` write the signal without checking, as before.

## Session browse `--include-archives`

`meridian session browse --include-archives` was deprecated and had no effect; it is
now removed (see [Session browse `--include-archives` removed](#session-browse---include-archives-removed)). It
duplicated Status: Archived: the picker's `/` transcript search now covers the
rows the Status view shows, so switch Status to Archived (Tab, then left/right) or
start with `--status archived` to search archived chats. It never searched ZIP
contents and still does not.

## Hook event names

Hook event names now identify the persisted moment when they fire. This release
opened a one-minor compatibility window in which the binary accepted both the old and
new names, warning once for each row that still needed migration. The window has closed.

| Old | New |
| --- | --- |
| `spawn.start` | `spawn.started` |
| `spawn.running` | `spawn.started` |
| `spawn.finalized` | `spawn.finished` |
| `work.start` | `work.activated` |
| `work.started` | `work.activated` |
| `session.responded` | `notify.your_turn` |
| `session.cache_expiring` | `notify.cache_expiring` |

The old names and keys are now rejected; see
[Removed hook names and keys](#removed-hook-names-and-keys).

`work.created` is a separate new event that fires only when `work start`
creates an item. `work.activated` fires whenever `work start` or `work switch`
resolves an item, even without a Meridian session, and when a primary launch
attaches `--work` or work carried by `--from`. Inherited child work, rename
repointing, and clear do not fire it. The `git-autosync` defaults are now
`spawn.finished`, `work.activated`, and `work.done`, so switching or starting
work syncs. A primary launch with attached work performs one synchronous sync
after creating its session and before starting the harness.

Hook payload `schema_version` 3 marks this contract change and adds `spawn.kind`
and `MERIDIAN_SPAWN_KIND`. The Plugin API carries the renamed event literal,
removes the impossible `when.status = ["skipped"]` value and the inert
`failure_policy` field/type, exports `SpawnKind` and `SpawnStatus`, and adds
`HookContext.spawn_kind`. It also removes `HookContext.from_authority`; package
code that constructed contexts through it should call `HookContext.from_roots`
with the authority's `project_root` and `runtime_root`. The new `when.kind` list
accepts `child`, `primary`, and `streaming`.

`failure_policy`, `when.status = ["skipped"]`, and a condition its event cannot
supply were accepted with a warning in the same window and are now rejected too.

For an unevented builtin row, Meridian enables only default events that can
match its `when` conditions. Incompatible defaults remain as disabled
overrides, so the higher-precedence row still suppresses lower rows with the
same name and event. For example, an unevented `git-autosync` row with
`when.status = ["success"]` runs only for `spawn.finished` and disables its
`work.activated` and `work.done` registrations. It errors, with a prompt to set
`event`, only if no default event can match.

The corresponding lifecycle/telemetry producer names changed from
`spawn.queued` to `spawn.created`, `spawn.running` to `spawn.started`, and
`work.started` to the separate `work.created` and `work.activated` moments.
Nonterminal spawn moments go to lifecycle observers and hooks, not the sparse
local `meridian telemetry query` projection; that projection continues to keep
`spawn.process_exited` plus terminal status events. Work telemetry remains
best-effort process telemetry, while work and session files are authoritative.

No state migration is needed. On mixed-version machines sharing one config, the
new binary accepts both name sets during this window, but an old binary still
rejects the new names. Keep the old names until every machine has upgraded,
then rename them before the next minor release. Runtime lifecycle dispatch
continues to drop truly invalid rows while valid and deprecated rows run; work
commands keep their core mutation even if dispatch setup itself fails.

## Compatibility policy and state version checks

Meridian now keeps renamed config keys, hook names, and CLI flags working with a
replacement warning for one minor release. State shared across builds uses an
integer format version where another build can read it; rebuildable indexes put
that version in the file name.

What users may notice:

- Work items whose `__status.json` contains an unknown field keep it through
  later metadata updates and warn once per file per command. A status file from a
  newer format is displayed as `requires-newer-meridian`, warns with its path and
  asks for an upgrade; commands validate every active and archived copy before
  renaming, deleting, moving, or rewriting anything. Other malformed status files
  now warn before using directory-derived defaults.
- `meridian telemetry query` and session replay skip records carrying an
  unsupported `v` and warn instead of interpreting them with the current schema.
  Session writes that read existing records to decide what to append (starting a
  chat, binding a native session, deduplicating model records, adopting an
  observed native session) refuse when an unsupported record names the same chat
  or native session, rather than writing stale state or creating a duplicate
  chat. Other session updates append without reading the journal. A newer record
  that names no chat or native session is invisible to those scoped decisions; it
  refuses only decisions that check every chat, such as repair binds. The
  one-time native-session import reads only the chats it binds; an unsupported
  record in one of them defers the import with a message instead of failing the
  command. The disposable history index skips unsupported session rows, keeps
  indexing later ones, and remembers the skipped versions so every indexed read
  warns once per command. Its file name includes both the index schema and
  session format (`history-v10-sessions-v1.sqlite3`) so a format change rebuilds
  from authority.
  Idle files are the deliberate exception to refusal: they are short-lived and
  expected to be absent, and refusing one after a mid-session upgrade would
  disable idle handling; they keep absent-state behavior and now warn with the
  file path.
- `meridian.plugin_api.__version__` is removed because the API is not installed
  independently; callers that need a version should use Meridian's package
  version. Launch-policy snapshot `schema_version`, Pi `pi_runtime_meta.json`
  `schema_version`, and the duplicate native-search SQLite metadata are removed
  because their released readers default or ignore them. Existing files that
  contain these state fields remain readable. The legacy native-import marker
  keeps its `schema_version`: released repair readers still require it.

No state migration or manual index rebuild is required. The first indexed read
builds the new input-versioned file automatically. Work status writers do not emit
`v` yet, so released strict readers continue to accept files this version writes.
When rolling back to v0.10.29, authority remains readable because this version
still writes the released work and session shapes. Its old `history-v10.sqlite3`
was not updated while the newer input-versioned index was active; run
`meridian session index rebuild --metadata-only` after rollback before relying on
indexed session discovery. Rollback support for other state changes remains
specific to their topic below.

## Notifications are hook events

Idle pushes, cache warnings and `meridian notify` fire the hook events
`notify.your_turn`, `notify.cache_expiring` and `notify.manual`. A built-in
`notify` hook delivers them through the `[notify]` backends, so routing,
silencing and extra channels are `[[hooks]]` rows; see
[Routing and extending notifications](configuration.md#routing-and-extending-notifications).
With no hook config, delivery is unchanged.

- **CLI.** `meridian notify "<title>" ["<body>"]`: the title is positional and
  required, the body optional, and the session headline always ends the body.
  `--title` is gone; replace `meridian notify "msg" --title T` with
  `meridian notify T "msg"`. `--no-email`, `--priority` and `--json` are
  unchanged, but the results list one entry per route (the default route is
  `notify`) rather than one per transport. Sent text names both, such as
  `notify (ntfy)`, and each JSON result has a `transports` list; a failed
  route's error still names each failed transport. The session label moves from the report to the
  top-level `label` key. For compatibility with the former one-argument form,
  a multi-line first argument uses its first non-empty line as the title and
  prepends the remaining lines to the body. Repeated attachment paths that
  resolve to the same file are sent once.
- **`command` backend removed.** `push_backend = "command"` and
  `email_backend = "command"`, with `push_command`, `email_command` and their
  `MERIDIAN_NOTIFY_*_COMMAND` variables, are gone. Move the script to a command
  hook on the notification events: it gets the notification as JSON on stdin
  (`notification.title`, `body`, `priority`, `label`, `attachments`) and in
  `MERIDIAN_NOTIFICATION_*` variables, rather than title and body as arguments.
  The removed `command` value, and every other unknown `push_backend` or
  `email_backend`, is now rejected when config loads instead of failing during
  delivery.
- **`--no-email` covers every email backend.** A notification that does not
  want email now skips `smtp` and `gmail` even when a route names them.
- **Hooks switch.** `MERIDIAN_HOOKS_ENABLED=false` now also turns off every
  notification. Hooks, notification routes included, now run in a directory
  with no Meridian runtime state (`meridian hooks run` too); only
  `interval`-throttled hooks skip there.
- **Hook rows.** `meridian hooks list` drops the `registration` column; the
  `default` source marks Meridian's own routes. Unnamed `git-autosync` rows are
  named from `remote`/`path` only, so a row that also sets `conflict_policy`
  gets a new name. Unknown `options` keys and unknown notification channel
  names on a builtin row are config errors. The `notify` builtin is rejected
  on non-notification events. During notification emission, invalid rows are
  dropped and named in the result warning; valid rows, including silencing and
  command rows, remain, and the ordinary default-route overrides still apply.
  Hook inspection exits non-zero and names every invalid row.
- **ntfy.** The `Tags` header is now the event name; update filters that
  matched the old `kind` values. When ntfy accepts the text message but rejects
  attachments, it attempts the rest and the route is sent with a warning naming
  every failed file; do not retry the already delivered text. Email remains
  all-or-nothing: one rejected attachment fails that email and its text.
- **Hook payload schema 2 (historical contract).** `HookContext.runtime_root`
  became nullable, `HookContext.notification` was added, and hook payload schema
  2 added the notification object. Those events were then named
  `session.responded`, `session.cache_expiring`, and `notify.manual`; payload
  schema 3 applies the current `notify.*` names described in
  [Hook event names](#hook-event-names).

No state migration is needed.

## History index schema 10

No authority migration is needed. The rebuildable history index moves from
schema 7 to 10 so an unstopped session generation becomes inactive when a later
generation of the same chat starts. Schema numbers 8 and 9 are skipped because
unreleased builds wrote projections in those namespaces.

The first indexed read rebuilds `history-v10-sessions-v1.sqlite3` from authority
and may take longer. Older `history-v7.sqlite3`, `history-v8.sqlite3`,
`history-v9.sqlite3`, and pre-policy `history-v10.sqlite3` files are left untouched
and can be removed after rollback is no longer needed.

## Idle compaction removed

Meridian no longer compacts idle sessions. Idle handling now sends only the
waiting push and the cache-expiry warning. Harness-native compaction is
untouched: Claude Code 2.1.299+ (`/config` → Idle compaction), Codex, OpenCode,
and Pi still compact on their own, and Meridian leaves those settings alone.

Remove these settings. File keys are ignored with an "unknown config key"
warning; environment variables are ignored silently:

- `[idle]` keys `compact`, `compact_minutes`, `compact_min_growth_tokens`
- `[harness.<harness>.idle]` keys `compact`, `compact_min_growth_tokens`
- `MERIDIAN_IDLE_COMPACT`, `MERIDIAN_IDLE_COMPACT_MINUTES`,
  `MERIDIAN_IDLE_COMPACT_MIN_GROWTH_TOKENS`, `MERIDIAN_HARNESS_IDLE_COMPACT_<H>`,
  `MERIDIAN_HARNESS_IDLE_COMPACT_MIN_GROWTH_TOKENS_<H>`

`meridian idle done` is gone. `meridian idle fire` accepts only `push` or `warn`
and no longer takes `--busy`, `--agents-running`, `--context-tokens`, or
`--harness-autocompact-off`. `meridian idle arm` no longer takes
`--context-tokens`; adapters report running work with
`--background-tasks '[{"kind": "agent|shell|watcher|other", "command": "..."}]'`.
The `idle status` table drops its COMPACT column. Compaction notices
(`[compacted]`, `[compaction failed]`, `[compaction skipped]`) no longer appear.

A Claude primary now holds the waiting push while subagents, workflows, or
`meridian spawn` shells run, and the cache warning arms even then; see
[running work](configuration.md#running-work).

Idle files under `MERIDIAN_HOME/idle/` lose the `context`, `compact_eval` and
other compaction fields. Files written by an older build no longer validate and
are treated as absent: an idle stretch open across the upgrade starts fresh on
the next turn, so its pending push or warning may not fire. No migration is
needed. Stop active primaries before switching versions in either direction.

## Idle compaction growth floor

Idle compaction was later removed; see
[Idle compaction removed](#idle-compaction-removed).

Replace `idle.min_compact_tokens` (and per-harness equivalents) with
`idle.compact_min_growth_tokens`, or remove it to use the calculated break-even.
The environment override is `MERIDIAN_IDLE_COMPACT_MIN_GROWTH_TOKENS`.
The value now measures growth beyond a baseline, not total context size.

Stop active Meridian primaries before changing versions. Idle files under
`MERIDIAN_HOME/idle/` gain a nested `context` baseline and a stretch-local
`compact_eval`. Existing released files acquire the baseline on the next
trustworthy turn; experimental flat-baseline files are discarded by validation
and re-created on the next arm. No migration or index rebuild is required.
Provider transcripts, chats, and spawn authority are unchanged.

Rollback requires restoring the old config/environment key and restarting
primaries. Released 0.10.0 cannot read the new idle rows: its status returned
an empty list against scratch copies of these rows. It will start fresh idle
stretches rather than preserve baselines or evaluations. Do not run both
versions against the same idle directory.

## OpenCode 2.x idle

Idle push, warning, and compaction now work on OpenCode 2.x; before, no OpenCode
session ever armed. Sessions on the `openai` provider are scheduled against an
estimated 30-minute cache (GPT-5.6 or later; per provider, not per model, so set
`[harness.opencode.idle] ttl_seconds` for older models). Other providers and
OpenCode 1.x stay push-only. A configured `ttl_seconds` always wins. See
[OpenCode versions](configuration.md#opencode-versions).

## Ignoring direnv

`[environment] ignore_direnv = true` opts harness launches out of inherited
direnv changes. The default remains `false`; upgrading alone changes no launch
environment. CLI and env overrides are described in
[configuration](configuration.md#ignoring-direnv-during-harness-launches).

No migration or index rebuild is required. Captured launch requests gain an
optional `ignore_direnv` field, and config snapshots gain the `environment` table.
Existing requests without these fields retain normal inheritance. Spawn/session
authority schemas and provider transcripts are unchanged.

Before enabling the setting, ensure direnv is installed and that harness binaries
remain available on the pre-direnv `PATH`. Start a new Meridian session to apply
it; existing processes keep their environment.

Rollback to 0.9.7 can read the additive request/config snapshot fields but ignores
the new policy. This was checked using the released 0.9.7 package against a scratch
JSON fixture, without starting a harness. Stop active launchers before switching
versions, and remove the new config setting/CLI flags when rolling back. The
direnv inheritance protection is unavailable on the older version.

## Current-chat tracking

Claude continuation follows the run's last observed native session, including
in-process `/clear` or `/resume`, without requiring a final-exit callback. The
original chat keeps its native identity. A separate `current_chat_id` selects the
current conversation: reuse an existing chat unchanged, or create a fresh chat
with the current chat's settings and work association. History, identities and
live leases are not copied. A normal fresh launch still starts fresh; only
continuation follows the previous run. Meridian follows a native `/resume` on a
best-effort basis; `meridian --continue <chat>` and `meridian session browse` are the
reliable ways to resume, because they record which run owns the chat.

The same pointer carries a verified post-exit session (Pi): Meridian proves the
native transcript before reusing or creating that chat, then publishes it as
current. A 0.9.4 spawn whose `run_boundary` names a verified `exit_chat_id` is
read as that run's current chat and rewritten without the old field on its next
update; its continuation target does not change. The retired exit field is
validated before removal; invalid evidence is not promoted to a current pointer.

`pN` inherits settings and work from the selected chat's current generation,
just like an explicit `cN`, even when no startup hook ran. It is not a frozen
snapshot of the original run's policy or fork source generation. New launches
record their final policy on the conversation before native execution; older
generations use their recorded run link, never a newest-owner guess.

Observation does not prove that a transcript was saved. Actual resume and read
operations still validate the exact native transcript and refuse missing or
invalid sources rather than silently continuing an older conversation. Disabled
or missing hooks cannot reveal an unobserved switch. Existing runs are not
retroactively tracked; restart Claude through the updated Meridian.

**Before upgrading, stop active Meridian launches and back up the affected
project runtime directories.** The new spawn field is incompatible with older
strict readers, including released 0.9.3 and 0.9.4. Do not overlap old runners with new
writers. History projection version 7 is rebuilt from authoritative files in its
own namespace; it does not migrate the older SQLite file in place.

**Rollback is not an in-place downgrade.** On copied runtimes, released 0.9.3 and
0.9.4 both reject a spawn containing `current_chat_id`. Restore the pre-upgrade runtime
backup before using the older release, then rebuild its metadata index with
`meridian session index rebuild --metadata-only`. That backup does not contain
Meridian records created after the upgrade. Provider-owned native transcripts
are not rewritten by this feature; keep them when restoring Meridian state.

## Upgrading to 0.9.3: idle notifications and compaction

Interactive primaries launched by Meridian now watch for idle time: a phone
push one minute after a turn lasting at least two minutes, or after 10 minutes
of silence following a quicker turn; a push and email 15 minutes before the
prompt cache expires; and a guarded compaction 5 minutes before expiry so the
eventual return is cheap. Spawns get none of this. Agents can also ping you on
purpose with `meridian notify "<message>"`. Configuration, defaults per harness
and every environment variable are in [configuration.md](configuration.md); the
per-harness mechanics are in [harness-integration.md](harness-integration.md).

Interactive Pi primaries now run with `PI_CACHE_RETENTION=long` unless that
variable is already set. For recognized Anthropic and OpenAI providers this
makes Pi's warning and compaction stages fit. `openai` is scheduled against 30
minutes, because OpenAI's `24h` retention typically lasts about that long.
One-hour Anthropic cache writes cost more than five-minute writes. Export
`PI_CACHE_RETENTION=short` to keep Pi's default retention and spend.

### What changed, and why

- A new state directory, `~/.meridian/idle/`, holds one small JSON file per
  primary session (`<harness>-<session>.json`): the current idle stretch and
  which notifications and compactions already happened, so a reloaded adapter
  never notifies twice. Files are written atomically and deleted after 7 days.
- New config tables `[notify]`, `[idle]` and `[harness.<harness>.idle]`, each key
  with a `MERIDIAN_NOTIFY_*`, `MERIDIAN_IDLE_*` or `MERIDIAN_HARNESS_IDLE_<KEY>_<H>`
  environment variable. Environment beats files at every level; within a level
  the per-harness key wins.
- Every session Meridian launches gets `MERIDIAN_SESSION_ROLE=primary|spawn`.
  The Pi-only `_MERIDIAN_PI_SESSION_ROLE` is removed.
- Interactive Claude primaries load a bundled mod (`--plugin-dir`), interactive
  Pi primaries load a fourth extension bundle, interactive Codex primaries get a
  `notify` hook on the app-server, and OpenCode primaries run a sensor inside
  Meridian's launcher.

### Before you upgrade

- Nothing to migrate. If any of your tooling reads `_MERIDIAN_PI_SESSION_ROLE`,
  switch it to `MERIDIAN_SESSION_ROLE` (values `primary` and `spawn`).
- Close interactive Pi sessions: a running Pi process keeps its loaded bundles,
  and the idle bundle loads at session start.

### After installing

- Compaction is **on by default** for interactive primaries. To turn it off for
  one tmux session, export `MERIDIAN_IDLE_COMPACT=0` before launching; for
  everything, set `[idle] compact = false`; for one harness,
  `[harness.codex.idle] compact = false`. Compaction may discard unsent text in
  the prompt box. It does not run while agents are running, under about 40k
  tokens of context, when the timer fired late (machine asleep), or when the
  harness's own auto-compaction is off.
  [Version 0.10.6](#idle-compaction-growth-floor) replaced the
  fixed context floor with growth since a baseline compared against each route's
  break-even.
  Idle compaction was later removed entirely; see
  [Idle compaction removed](#idle-compaction-removed).
- Notifications are silent until configured: set `[notify] ntfy_topic` for push
  and `email_to`, `smtp_user` and `smtp_password_file` (a `0600` file holding
  a Gmail app password) for email. `MERIDIAN_NOTIFY_SMTP_PASSWORD` works as a
  fallback but is inherited by every spawn and can land in transcripts. Idle
  notifications name the agent, work item and tmux session and include short
  labelled user/assistant excerpts by default. Set
  `[notify] include_messages = false` to keep message text on the machine.
- Per-harness defaults: Claude senses its cache lifetime from the transcript
  (1 h or 5 min); Meridian gives interactive Pi primaries long cache retention;
  Codex assumes 30 minutes (`[harness.codex.idle] ttl_seconds = 1800`); OpenCode
  is push-only (`ttl_seconds = 300`).
  [Version 0.9.9](#opencode-2x-idle) gave OpenCode 2.x `openai`
  sessions warning and compaction; other providers and OpenCode 1.x remain
  push-only. Pi provider IDs other than `anthropic` and `openai` remain push-only.
  Version 0.9.8 added an estimated 30-minute warning and compaction schedule for
  Pi's `openai-codex` provider.
- Idle and cache-warning titles show time until the cache goes cold when a TTL
  is known. Heuristic/configured TTLs are labelled `est.`. Idle state saves
  `ttl_estimated` alongside the TTL; existing development-build records without
  that field are treated as estimates until the next turn re-arms them. No
  migration or repair command is needed.
- Claude sessions you start yourself (not through `meridian claude`) can opt in:
  `CLAUDE_CODE_PLUGIN_DIRS="$(meridian idle mod-path)" claude`.

### Rolling back

Rollback works. A 0.9 build ignores `~/.meridian/idle/` and logs
"Ignoring unknown Meridian config key" for the new tables while loading the rest
of your config (this is the 0.9 loader's observed behaviour for unknown tables;
the directory can also be deleted outright). Pi sessions started under 0.9 get
`_MERIDIAN_PI_SESSION_ROLE` again; nothing else reads `MERIDIAN_SESSION_ROLE`.
No data the harnesses keep is touched by this release.

## Pi spawned-run settlement

Managed Pi sessions now require a stable **Pi >=1.1.0 and <2**, for both native
TUI primaries and spawned RPC workers. Update Pi before launching either role.
Meridian rejects older, prerelease, unrecognized and newer-major versions during
runtime resolution, before model work. This matches the managed extensions'
peer contract; their development SDK is pinned to the qualified Pi 1.1.0.

The resolver also checks each role's required CLI flags. Spawned sessions still
validate the actual settlement event: missing/non-Boolean `aborted` fails closed
with `pi_invalid_agent_settled`. Pi 1.0.4 lacked that field; it is now refused at
launch rather than after spending model work.

A tool batch ending no longer means the parent is idle. Meridian waits for
native settlement and any open compaction; retries and queued continuations can
finish before completion is chosen. Active work clears the idle result-delivery
window, while genuinely idle undeliverable results still have a bounded failure.
This changes no private-file schema. Restart workers to load the new Python code;
do not replace their installed environment while they are running.

## Upgrading to 0.9: Pi task ownership and result delivery

Before reinstalling, finish or cancel managed shell tasks and spawned Pi runs,
then close Pi sessions loading Meridian's extensions. Start a new session after
installation: a running Pi process keeps its loaded bundles. Use one bundle
version per parent; old and new writers cannot safely share its private state.

Managed Bash now preserves task records across reload and restart. Same-process
`/reload` retains live process handles. After a process restart, previously
running tasks show `ownership_lost`; their recorded PID cannot prove ownership.
Inspect the old process manually, then use
`bash_manage({action: "detach", bash_id: "b-…"})` to release tracking. Detach
releases tracking; a live owner still cleans up its tasks on normal shutdown.
Kill and abort wait for owned POSIX process groups to exit before reporting
completion. Terminal history and consumption markers survive subsequent writes.

A terminal `bash_manage(wait)` persists `notification_consumed_at_ms` before
returning. Unattended background Bash and direct child results remain owed
until explicitly consumed or admitted as a specific native custom message.
Follow-ups wait for Pi to become idle, so an active tool wait can consume its
result before a completion notice enters the queue. `/ps` clear keeps unread
background completions; returned foreground results can be cleared.

Private coordination files under `pi-bash/<parent>/` now include:

- `delivery-receipts.json` (read-only since v0.18.0; `launch-receipts.json` replaces it,
  keyed by child and run): v1 parent and `messages`, mapping each delivery ID
  to its admitted work IDs. Queueing a message creates no receipt.
- `delivery-observations.json`: v1 parent and `observed_message_ids`, written
  by the Python runner when it observes that exact native admission event.
- `delivery-fault.json`: v1 parent, operation and error, preserving notification
  failures separately from shell execution failures.
- `observed-spawns.json` (retired in v0.18.0: neither read nor written here; the parent's
  `spawns/<parent>/observed-launches.json` replaced it): durable observed child IDs plus
  temporary wait leases bound to the caller's PID, birth time and expiry. Interrupted waits
  no longer suppress child completion permanently.

Valid existing v1 Bash records remain readable; no migration command is needed.
Missing receipt files leave unconsumed work eligible. The old
`last-notification.json` timestamp is ignored because it cannot identify which
result reached the model. A notice admitted by an older bundle may therefore
repeat once after upgrade. Present malformed or wrong-parent files block
completion with a bounded diagnostic rather than becoming empty work.
See the [delivery contract](../src/meridian/pi_runtime/.context/delivery-contract.md)
for exact shapes and writers.

Hot reload preserves queued message ownership. Cold restart retries terminal
work without a receipt; waited or admitted work remains consumed. Native
admission, disk receipts and public RPC observation are separate stores. If
a process crashes after persisting a receipt but before Python observes its
event, completion fails closed with
`pi_evidence_unreadable: pi_delivery_event_unobserved: <delivery-id>` after the
fixed recovery window. Inspect the previous native session and result, then
start a fresh parent/session. Keep the receipt; deleting it or inventing an
observation loses the evidence needed to explain the interruption. This does
not guarantee atomic exactly-once delivery across a process crash.

### Rolling back Pi bundles

Reusing this private parent state with 0.8.2 bundles is unsupported. A probe of
the actual 0.8.2 bundles against copied new state repeated both waited and
admitted notices, listed no recovered tasks, and discarded the old records on
its first write. Stop all writers before downgrading and use a fresh parent;
retain copies of task records and native transcripts for inspection. Reinstalling
the new version cannot recover history that an old writer has overwritten.

# Upgrading to 0.7

## What changed, and why

Each chat now stays bound to exactly one native Claude, Codex, OpenCode or Pi
session. Cursor chats are not bound. Logs, search, and archives use that harness's transcript instead
of a second Meridian copy. Search verifies matches against the transcript and
reports how many sources it searched. Meridian no longer writes `history.jsonl`;
those files accounted for 27 GB of the 29 GB in the author's
`~/.meridian/projects`.

## Before you upgrade

Upgrading is one-way. Once 0.7 runs a spawn in a project, 0.6.7 can no longer
list or read that project's spawns (see [Rolling back](#rolling-back-to-067)).

Check for work still running:

```sh
meridian spawn list
```

Let background or headless spawns finish, or cancel them before reinstalling.
Reinstalling replaces the tool environment underneath them, so they can die.
Interactive sessions survive the reinstall.

Install from PyPI:

```sh
uv tool upgrade meridian-cli
```

Or update a source checkout:

```sh
git pull
uv tool install --force . --no-cache --reinstall
```

## The first run

The first command in each project imports its existing chats once. Meridian
writes the import report under the runtime and prints a line like this to stderr:

```text
Imported native sessions for N of M existing chats; K left unbound (details: …)
```

Only chats with exactly one provable native session are bound. The rest stay
`[unbound]`; Meridian will not guess. On first indexed use, Meridian also builds
the schema-specific `history-index/history-v6.sqlite3` from authoritative files.
It takes a second or two, up to about 5 s on a 5.5 GB runtime. The 0.6.7 index,
`history-index/history.sqlite3`, is left alone, so a 0.6.7 process that survived
the reinstall can still finish. A spawn started by a still-running 0.6.7 process
after 0.7 built `history-v6.sqlite3` may not appear in `spawn list` or
`session browse` until you run `meridian session index rebuild` once.

## After upgrading

Run repairs after old processes have finished:

```sh
meridian doctor
```

Doctor catches chats that an older process was still writing during the
upgrade. `doctor` also binds old Pi chats once, when it can prove a match. (The
next interactive `meridian` launch does the same in the background.) A spawned
Pi chat is bound only when its spawn's session directory holds exactly one
session that matches the spawn's cwd and start time, and whose first message is
the spawn's prompt. When the prompt was not kept, the session's last reply must
match the report instead. Primaries shared one session directory in 0.6.7, so
they are never bound automatically. When doctor binds any, it prints a line
like:

```text
repaired: legacy_pi_sessions
legacy_pi_sessions: bound 63 old Pi chat(s) to their native sessions; 513 left unbound; inspect one with `meridian session repair cN` (e.g. c668)
```

On copies of the author's runtimes, this bound 155 of 770 old Pi chats. Most
of the rest had no recorded link to a spawn, or were primaries. The pass runs
once per chat; a second `doctor` reports nothing new.

Find chats that are still unbound. Primary chats show `unbound` in the `NATIVE`
column of:

```sh
meridian session browse --plain --limit 1000
```

Spawned chats do not appear there. The import report named in the first-run line
(`details: …`) lists every chat the import left unbound, and `doctor`'s
`legacy_pi_sessions:` line names one to start with.

Unbound means Meridian cannot prove which native conversation belongs to that
chat. If you know the native transcript path, you may still be able to read it
with `meridian session log --file PATH`. An unbound chat can never be resumed or
forked.

Inspect a chat and its candidate native sessions:

```sh
meridian session repair c123
```

For an unbound chat this is read-only. Each candidate lists its path, session
ID, header cwd and time, first-user-message excerpt, and whether cwd, time
window, prompt, or another binding matches. It prints the exact command to bind
a candidate. For a chat already bound, it prints the binding and says there is
nothing to repair.

Bind only after checking the evidence:

```sh
meridian session repair c123 --native /path/to/native-session
```

This binds with source `user_repair`. It supports native sessions from Claude,
Codex, OpenCode, and Pi. Meridian refuses if the chat is already bound, the file
is not a valid native session for that harness, another chat already owns the
native session, or the chat already records a different session ID. A cwd
mismatch or a session outside the time window requires `--force`:

```sh
meridian session repair c123 --native /path/to/native-session --force
```

Bindings are immutable: `--force` does not let you replace an existing binding.

## Keep transcripts you care about

Meridian no longer keeps its own copy of a conversation; the harness's
transcript is the only one. Harnesses can delete their own transcripts:
Claude removes sessions older than `cleanupPeriodDays`, which defaults to 30
days. Once a harness deletes a file, that chat reads as
`native_transcript_missing`.

To keep history longer, do either or both:

- Raise Claude's retention in `~/.claude/settings.json`, for example
  `{"cleanupPeriodDays": 365}`.
- Archive regularly. `meridian session archive --eligible --apply` captures
  each selected chat's exact native transcript into the archive ZIP, and
  those snapshots stay readable after the harness deletes the original.

## Archives made before 0.7

`meridian session import` and `meridian session restore` accept archives
written by 0.6.7. Those archives hold Meridian's old runner copy
(`history.jsonl`) rather than the harness transcript, and 0.7 does not read
that copy. So `session log` on an imported or restored 0.6.7 record says:

```text
error: c1 is historical and has no retained native snapshot
```

An archived chat that is still bound to its harness's own transcript reads
normally with `meridian session log cN` for as long as the harness keeps that
file. Otherwise, the old copy is still inside the ZIP at
`meridian-history-v1/records/<history-id>/aggregate/history.jsonl`; extract
it with `unzip` if you need it. Archives made by 0.7 contain the native
transcript and read back normally.

## Reclaim old runner files

Preview what can be removed:

```sh
meridian session archive --prune-runner-history
```

The command is a dry run. Add `--apply` to delete the listed files. By default,
it considers spawns finished more than 14 days ago; set `--after-days N` to
choose another age:

```sh
meridian session archive --prune-runner-history --after-days 30
meridian session archive --prune-runner-history --after-days 30 --apply
```

It removes only the retired runner streams (`history.jsonl` and
`last-observed-event.json`, including their attempt copies). A spawn must be
terminal, old enough, and have an exact readable native transcript for every
run source. Unresolved exits, missing transcripts, running spawns, and live
managed scopes are skipped. Meridian keeps native transcripts, reports,
lifecycle and control state, and all other files under each spawn. The command is explicit
and never runs automatically. `meridian doctor` does not delete these files.
`meridian doctor --prune` is different: it deletes whole spawn folders idle for
more than `state.retention_days` (30 by default), including each spawn's
record, prompt, report and any runner copy. The chats' native transcripts are
not touched.

## Spawn file listing

`meridian spawn files` was deprecated because no harness produced its manifest; it
is now removed (see [Spawn file listing removed](#spawn-file-listing-removed)).

## Spawn message rename

`meridian spawn inject` is now `meridian spawn message`. Scripts using the old
spelling keep working: it warns on stderr and runs the same command until the
next minor release. Each message also opens with a `[Meridian]` line naming the
sending spawn and chat and, when the sender can receive one, the
`meridian spawn message <sender chat ID>` command that answers it, with
`-C <sender project root>` when the sender is in another project. Anything that parses the
delivered text sees that line first; the text you sent follows a blank line.

## Behavior changes you'll notice

- `meridian session log` reads the native transcript. A `pN` view names its
  chat; for example: `p3 → c3 (entry chat; run predates exit tracking)` for a
  0.6.7 run.
- Session-search text output includes coverage. A complete run can say
  `Searched 1 sources (complete).`; incomplete sources are called out.
- `--fork` on a harness that cannot create a new native session now refuses.
  Cross-harness `--continue` also refuses instead of silently switching
  harnesses or starting fresh.
- `--prompt-file` and `-p` now reach interactive Claude, Pi, and OpenCode
  sessions.
- Pi and Claude runs can report verified exit-session changes. Missing exit-identity evidence
  is not a process failure: primary completion output shows the process result
  and the continue command without an `exit unresolved` line. `spawn show`
  reports `Exit-session identity: unverified` when proof is absent; JSON keeps
  the `unresolved` status. Identity mismatches and possible session changes
  still produce warnings. The recorded entry chat remains the continue target
  unless Meridian verifies an exit chat.
- `spawn show --json` includes `chat_id`, `continue_chat_id`, and `run_boundary`.
- `meridian session log --file history.jsonl` is refused: runner history is not
  a native transcript.
- `session search --include-archives` is removed. Corpus search includes bound
  archived chats by default; it does not search ZIP contents. The separate
  `session browse --include-archives` option remains for browsing archived rows.
- Guardrail scripts now receive `_MERIDIAN_GUARDRAIL_REPORT` and
  `_MERIDIAN_GUARDRAIL_CHAT_ID`; `_MERIDIAN_GUARDRAIL_OUTPUT_LOG` is removed.
- Archive ZIPs contain the exact native transcript as `native-transcript.jsonl`,
  not a runner-history copy. Import and restore read the archived snapshot, so
  its transcript remains available after transfer or restore.

## Known limits

- Claude session switches need a final launch-correlated hook receipt and a valid
  transcript. If hooks are disabled or no final receipt is written, continuation
  falls back to the entry chat; it cannot guess a post-`/clear` destination.
- Codex rollouts that Codex moved to `archived_sessions/` are not read yet, so
  those chats stay unbound; see
  [#528](https://github.com/haowjy/meridian-cli/issues/528).
- If the harness had already deleted an old chat's native file, the chat stays
  unbound and Meridian cannot display it. Its 0.6.7 runner copy,
  `spawns/pN/history.jsonl` under the project runtime, stays on disk (the prune
  skips it) until `meridian doctor --prune` removes the spawn folder. Read it
  with `jq` if you need it.

## Rolling back to 0.6.7

Downgrading is not supported. Once 0.7 has run a spawn in a project, 0.6.7
cannot read that spawn's state: `spawn list` fails for the whole project with
`Invalid authoritative history metadata`, and `spawn show` and `session log`
fail too. A 0.6.7 metadata rebuild stops at the first new row. The native
harness transcripts and spawn reports stay on disk, but only 0.7 can show them
through Meridian.

Only if you ran a 0.7 pre-release build: it may have migrated
`history-index/history.sqlite3` in place, making that index incompatible with
0.6.7. With old processes stopped, delete `history-index/history.sqlite3*`.
That fixes only the index; it does not make 0.6.7 understand rows 0.7 wrote.

See also [History storage](history.md), [Commands](commands.md), and
[Troubleshooting](troubleshooting.md).
