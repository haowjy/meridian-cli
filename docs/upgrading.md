# Upgrade notes

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

`meridian session browse --include-archives` is deprecated and has no effect. It
duplicated Status: Archived: the picker's `/` transcript search now covers the
rows the Status view shows, so switch Status to Archived (Tab, then left/right) or
start with `--status archived` to search archived chats. It never searched ZIP
contents and still does not. The flag is still accepted and prints a warning on
stderr; it becomes an error in the next minor release. Nothing needs doing before
or after upgrading.

## Hook event names

Hook event names now identify the persisted moment when they fire. This release
opens a one-minor compatibility window: the new binary accepts both the old and
new names, while warning once for each row that still needs migration.

| Old | New |
| --- | --- |
| `spawn.start` | `spawn.started` |
| `spawn.running` | `spawn.started` |
| `spawn.finalized` | `spawn.finished` |
| `work.start` | `work.activated` |
| `work.started` | `work.activated` |
| `session.responded` | `notify.your_turn` |
| `session.cache_expiring` | `notify.cache_expiring` |

Run `meridian hooks check` after upgrading. It exits successfully when these
are the only findings and lists each deprecated row with its replacement.
`meridian hooks list` shows the registration on the new event and marks it
`deprecated`. Rename rows at leisure during this window; the old names stop
loading in the next minor release.

A row that still uses an old name is registered on the new event, but receives
its configured old name in `HookContext.event_name`, JSON `event_name`, and
`MERIDIAN_HOOK_EVENT`. This keeps existing command scripts working while their
config is migrated. A row configured with the new name receives the new name.

Rows configured with `spawn.start` or `work.start` were accepted previously but
never fired. They now register on the live `spawn.started` and `work.activated`
events, so the warning also says that the row will begin running.

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

The corresponding config transitions share the one-minor window:

- `failure_policy` loads with a warning, has no effect, and will be rejected in
  the next minor release. Hook failures always log and continue.
- `when.status = ["skipped"]` warns and drops `skipped`; any other status values
  in the list continue to filter normally. If no status remains, the row is
  skipped instead of widening to match every status.
- A row that explicitly names an event but sets a condition that event cannot
  supply warns that it can never match and is skipped. In the next minor release
  the same row becomes a config error. Because the skipped row does not register,
  it no longer shadows a lower-file row with the same name and event.

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
continuation follows the previous run.

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

- `delivery-receipts.json`: v1 parent and `messages`, mapping each delivery ID
  to its admitted work IDs. Queueing a message creates no receipt.
- `delivery-observations.json`: v1 parent and `observed_message_ids`, written
  by the Python runner when it observes that exact native admission event.
- `delivery-fault.json`: v1 parent, operation and error, preserving notification
  failures separately from shell execution failures.
- `observed-spawns.json`: durable observed child IDs plus temporary wait leases
  bound to the caller's PID, birth time and expiry. Interrupted waits no longer
  suppress child completion permanently.

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

`meridian spawn files` is deprecated: no harness produced its manifest, so it
always returned an empty result. It still prints that empty result, with a
warning on stderr, and becomes an error in the next minor release. Remove it
from scripts, including `meridian spawn files <id> | xargs git add`; stage
changes with `git add` directly.

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
