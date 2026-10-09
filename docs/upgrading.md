# Upgrade notes

## Upgrading to 0.9.1: idle notifications and compaction

Interactive primaries launched by Meridian now watch for idle time: a phone
push after about a minute, a push and email 15 minutes before the prompt cache
expires, and a guarded compaction 5 minutes before expiry so the eventual
return is cheap. Spawns get none of this. Agents can also ping you on purpose
with `meridian notify "<message>"`. Configuration, defaults per harness and
every environment variable are in [configuration.md](configuration.md); the
per-harness mechanics are in [harness-integration.md](harness-integration.md).

Interactive Pi primaries now run with `PI_CACHE_RETENTION=long` unless that
variable is already set. For recognized Anthropic and OpenAI providers this
makes Pi's warning and compaction stages fit; OpenAI is scheduled against a
30-minute cache (its typical `24h` lifetime), while one-hour Anthropic cache
writes cost more than five-minute writes. Export `PI_CACHE_RETENTION=short` to
keep Pi's default retention and spend.

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
  `[harness.codex.idle] compact = false`. Compaction never runs over a draft in
  the prompt box, while agents are running, under about 40k tokens of context,
  when the timer fired late (machine asleep), or when the harness's own
  auto-compaction is off.
- Notifications are silent until configured: set `[notify] ntfy_topic` for push
  and `email_to`, `smtp_user` and `smtp_password_file` (a `0600` file holding
  a Gmail app password) for email. `MERIDIAN_NOTIFY_SMTP_PASSWORD` works as a
  fallback but is inherited by every spawn and can land in transcripts.
- Per-harness defaults: Claude senses its cache lifetime from the transcript
  (1 h or 5 min); Meridian gives interactive Pi primaries long cache retention,
  with OpenAI scheduled against a 30-minute cache (its typical `24h` lifetime);
  Codex assumes 30 minutes (`[harness.codex.idle] ttl_seconds = 1800`); OpenCode
  is push-only (`ttl_seconds = 300`). Pi provider IDs other than `anthropic` and
  `openai` remain push-only.
- Claude sessions you start yourself (not through `meridian claude`) can opt in:
  `CLAUDE_CODE_PLUGIN_DIRS="$(meridian idle mod-path)" claude`.

### Rolling back

Rollback works. A 0.9 build ignores `~/.meridian/idle/` and logs
"Ignoring unknown Meridian config key" for the new tables while loading the rest
of your config (this is the 0.9 loader's observed behaviour for unknown tables;
the directory can also be deleted outright). Pi sessions started under 0.9 get
`_MERIDIAN_PI_SESSION_ROLE` again; nothing else reads `MERIDIAN_SESSION_ROLE`.
No data the harnesses keep is touched by this release.

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
lifecycle and control state, and all other spawn files. The command is explicit
and never runs automatically. `meridian doctor` does not delete these files.
`meridian doctor --prune` is different: it deletes whole spawn folders idle for
more than `state.retention_days` (30 by default), including each spawn's
record, prompt, report and any runner copy. The chats' native transcripts are
not touched.

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
- Pi runs report verified exit-session changes. Other harnesses report
  `unresolved` when Meridian cannot prove the exit identity.
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

- Claude `/clear` can change sessions without a verified exit; see
  [#533](https://github.com/haowjy/meridian-cli/issues/533).
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
