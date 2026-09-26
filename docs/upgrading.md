# Upgrading to 0.7

## What changed, and why

Each chat now stays bound to exactly one native Claude, Codex, OpenCode, Pi, or
Cursor session. Logs, search, and archives use that harness's transcript instead
of a second Meridian copy. Search verifies matches against the transcript and
reports how many sources it searched. Meridian no longer writes `history.jsonl`;
those files accounted for 27 GB of 29 GB in one author's runtime.

## Before you upgrade

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

The first command imports existing chats once. Meridian writes the import report
under the runtime and prints a line like this to stderr:

```text
Imported native sessions for N of M existing chats; K left unbound (details: …)
```

Only chats with exactly one provable native session are bound. The rest stay
`[unbound]`; Meridian will not guess. On first indexed use, Meridian also builds
the schema-specific `history-index/history-v6.sqlite3` from authoritative files.
On large runtimes this takes about 1–5 seconds. The 0.6.7 index,
`history-index/history.sqlite3`, is left alone. A 0.6.7 process already running
can keep using it and finish its work.

## After upgrading

Run repairs after old processes have finished:

```sh
meridian doctor
```

Doctor catches chats that an older process was still writing during the
upgrade. Doctor and primary-launch background repairs also run a one-shot
recovery pass over old unbound Pi chats and bind only matches they can prove.
When doctor binds any, it prints
`repaired: legacy_pi_sessions` and a summary of how many it bound and left
unbound, with a pointer to `meridian session repair cN`.

Check for chats still unbound:

```sh
meridian session browse
```

`[unbound]` means Meridian cannot prove which native conversation belongs to
that chat. If you know the native transcript path, it may still be readable
directly with `meridian session log --file PATH`; an unbound chat is never
resumable or forkable.

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

This binds with source `user_repair`. It works for any harness. Meridian refuses
if the chat is already bound, the file is not a valid native session for that
harness, or another chat already owns the native session. A cwd mismatch or a
session outside the time window requires `--force`:

```sh
meridian session repair c123 --native /path/to/native-session --force
```

Bindings are immutable: `--force` does not let you replace an existing binding.

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

## Behavior changes you'll notice

- `meridian session log` reads the native transcript. A `pN` view names its
  chat; for example: `p3 → c3 (entry chat; exit identity unresolved)`.
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
- Exits on non-Pi harnesses remain `unresolved`; `pN` views use the entry chat.
- Codex `archived_sessions/` rollouts are not read yet; see
  [#528](https://github.com/haowjy/meridian-cli/issues/528).
- If the harness has deleted a chat's native file, the chat stays unbound.
- Some Pi primary chats created by 0.6.7 still need manual repair. Automatic
  recovery binds only a match it can prove; inspect candidates before binding.
- Warm search still includes about 0.8 seconds of CLI startup; see
  [#527](https://github.com/haowjy/meridian-cli/issues/527).

## Rolling back to 0.6.7

Keep using 0.7 if you need to read or search conversations created by it. A
0.6.7 process cannot read the new build's `run_boundary` and `native_store`
state rows. In a scratch test, the new build completed a one-turn Claude spawn
(`p1`, `succeeded`) and reported `ROLLBACK-NEW-067`. With a real isolated
0.6.7 install, `spawn list` failed with `Invalid authoritative history
metadata`; `spawn show p1` quarantined the new state row; `session log c1`
failed with the same history-index initialization error; and `session index
status` reported `History index: failed`. A 0.6.7 metadata rebuild also stopped
at the quarantined row. The old
build therefore cannot list, show, or read that new conversation through
Meridian. Keep using 0.7 for those rows; their native harness transcript and
report artifacts remain on disk, but 0.6.7 cannot provide Meridian's new
identity-aware session view.

The scratch test used a throwaway git project and runtime. It ran the new
command with `_MERIDIAN_RUNTIME_DIR` set to the scratch runtime, then ran each
old command with `uvx --isolated` and `MERIDIAN_PROJECT_DIR` pointed at the
scratch project. Do not use plain `uvx`: it can reuse the installed PR build.

0.7 leaves 0.6.7's `history-index/history.sqlite3` alone. Deleting
`history-index/history.sqlite3*` matters only if an earlier prerelease build
migrated that old index in place; it does not make 0.6.7 understand 0.7 state
rows. Stop old processes before removing an incompatible index. The rollback
probe did not have an old index database to remove, so deletion was not part of
its result.

For a safe test, point both versions at a disposable runtime and project. Never
use `~/.meridian/projects/*` as rollback-test data.

See also [History storage](history.md), [Commands](commands.md), and
[Troubleshooting](troubleshooting.md).
