# The machine inbox

Messages addressed to no session wait under `~/.meridian/machine/inbox/` until a session
takes one. Nothing drains it. `store.py` is the only writer of that directory. The harness
queues also called "inbox" (`harness/claude_inbox.py`, `harness/agy/inbox.py`) are a
different thing: each belongs to one session and empties itself.

## Layout

```
format        "1"; a build that finds another value reads and writes nothing
inbox.lock    held for every change; never unlinked
issued        highest number issued, one line; never goes down
pruned        time of the last prune pass
messages/m412.json    one message, written once, never changed, mode 0600
by-origin/<sha256 of via program, NUL, ref>  ->  ../messages/m412.json
taken/m412.json       who claimed it and when; its absence means "waiting"
offered/m412.json     who it was offered to; a record for `list`, it grants nothing
hook-log/m412.json    what the `inbox.received` rows did for it
```

A message's ID is its file name. Readers take no lock: every file appears whole or not at
all (`link(2)` or rename), and a file that doesn't parse is skipped and named on stderr.

## The sequences

Several processes write at once and any is killed between two system calls, so the order
of the steps is the design. Each runs under `inbox.lock`.

**Write** (`file_message`):

1. With a `via` program and ref, read `by-origin/<hash>`. It matches only when the file it
   names exists and that file's `via` has this program and ref. On a match return that ID
   and write nothing.
2. Issue the number: `issued` plus one, written back durably before anything uses it.
3. With a `via` ref, point `by-origin/<hash>` at the new message (staged link, rename).
4. Write the message: staged file, fsync, `link(2)`, directory fsync.

When the store has returned, `filing.file_from_command` calls `launch_inbox_received`,
also on a step 1 match: the first call may have died before it got there. The store
starts nothing itself, so it can be exercised with hooks configured.

What a kill leaves, and what the next call does:

| Killed after | Left behind | Repeat of the same call |
|---|---|---|
| step 2 | a number with no message | takes the next number; a gap, never a reuse |
| step 3 | a link to no file | step 1 reads no match; new number, link repointed |
| staging in step 4 | a `.create` name `list` ignores | as above; prune removes the name after an hour |
| the `link` | a whole message | step 1 matches and returns its ID |

`issued` is a counter and not a scan of `messages/`, so prune can delete any message
without a number coming back. If `issued` is missing or unreadable while message, claim or
offer files exist, the write stops: counting up from the files left could reissue a
pruned number.

**Take**: check the message file exists, then `link(2)` a claim to `taken/<id>.json`. If
the name exists, the same taker (`Claim.same_taker`: kind, machine, chat ID, and the `via`
program and sender) succeeds and anyone else is refused. **Release** unlinks the claim; a
message with no claim is already released, which is a success so a cut-off call can be
repeated.

**Offer** (`offer.offer`): read the spawn store and refuse a session that is not a
primary (an offer says "claim it", and only a primary may take), then under the lock check
the message exists and is not taken; release the lock; send the notice; take the lock
again, check once more, and append to `offered/<id>.json`. Send first, record second: a
crash between the two leaves a session that was told and no record, and the repeat tells
it again. The lock is never held across the send. The send is a function the caller
passes in: the CLI passes `cli/message.py:send_message`, which refuses a cold or stopped
session itself, and any other caller passes its own. The CLI only checks the caller's
role, parses and prints. `offer.taker_running` holds the rule for whether a chat's run is
live, which `list --taken` shows. `cli/message.py:_send_to_session` has a second copy of
that rule: changing one does not change the other.

**Prune** (`prune.py`): scans without the lock and takes it for each deletion, checking
again under it. A message taken more than 30 days ago is deleted in this order: message,
`by-origin` link, offer record, hook log, claim. The claim goes last so a kill part way
leaves records with no message, which the next pass removes, and never a message that
looks waiting again. A waiting message is never deleted. A claim that doesn't parse is
skipped, and the models refuse a time with no zone, so no file can raise in the scan and
stop the passes after it. It runs at most once a day (the
`pruned` stamp), from the detached hook process and from every `meridian inbox` command;
not at a primary launch, because the machine that runs a chat bot launches none.

## Who is calling

`caller.resolve_caller` is the one answer for `meridian message inbox` and `meridian inbox
list`, `read`, `take`, `release` and `offer`. It takes the invocation's `Caller`
(`get_global_options().caller`): the spawn ID, and the caller's project root when `-C`
named another project. It does not read `MERIDIAN_SPAWN_ID` itself, because `-C` into
another project removes the session's names from the environment before a command runs
(`cli/main.py`, `project_scoped_env_names`): a child running `meridian -C ../other inbox
take` would then find no session and pass for a terminal. An ID with no spawn record is refused, never treated as a
terminal. The session's chat ID comes from its record: `MERIDIAN_CHAT_ID` names the chat
that launched a child.

- A **terminal** (no session: a person, a daemon) sees everything and may do everything.
- A **primary** (`kind == "primary"`) may `take`, `release`, `offer` and pass `--all`.
- Any other session, `streaming` included, may `list` and `read`.

This stops a confused child, not one that forges its environment.

`caller.project_of_root` is the one definition of a session's project: the path of the
runtime root its record was found in, below `projects/`. The
caller, `offer`'s check on the receiving session and the launch line all use it. An ad-hoc
project (`adhoc.…`) counts as none. A session sees messages with its own project or with
none.

A root that gives no ID `fields.is_project_id` accepts refuses the command. An ID is in
the safe set and is one path component, with no `/`. That is narrower than the form of a
committed ID (`core/refs.is_project_id_form`, which `project_identity` and
`lib/machines/caller.py` apply). Two things produce a root that fails: `meridian.toml` can
set an ID with a `/` or a character outside the safe set, and `_MERIDIAN_RUNTIME_DIR` can
put a root anywhere. The command is refused because the
alternative is to read such a session as having no project, and its messages would then
be shown to every session.

A process no session runs has a project only from `--project-id`, never from its working
directory: a hook row runs in home, and a daemon's directory is not what its messages are
about.

## Other machines

The inbox is this machine's only; `<machine>/inbox` is not built. Three places refuse it,
each with a kind, and building it means changing all three:

- `core/refs.parse_ref` refuses `<machine>/inbox` (`REMOTE_INBOX_ERROR`), so
  `machine_forward.other_machine` never resolves a machine for it.
- `message/address.parse_address` refuses `inbox` with `--machine`, and `inbox` in a
  command another machine sent (`is_machine_call`): filed here it would be recorded as
  from a terminal on this machine.
- The `inbox` commands are not on the crossing list (`CommandDescriptor.crosses`), so
  `main` refuses `--machine` for them. `offer` hands its session to `locate_run` as
  typed, which refuses another machine's `<machine>/<id>`.

## What a sender controls

Meridian never stores or prints a command a caller supplied.

- `from`, `project` and the default `reply_to` are filled in from the caller. The
  session's agent name in `from` is a profile name, which a repository chooses, so it is
  held to `SAFE_FIELD` too; a name outside it is left out of the message.
- `--reply-to` must have the form of a message address; `read` and `take` build the reply
  command from it.
- `via` and `--project-id` are held to `SAFE_FIELD`. The models validate the same rules on
  read, so a file edited by hand fails to parse instead of reaching a reader.
- The text reaches a reader in two places. `list` shows `fields.preview`: line breaks and
  tabs to spaces, then categories Cc and Cf removed (this takes out the bidirectional controls),
  spaces collapsed, trimmed, 80 code points, printed as a JSON string after a fixed label.
  `read` and `take` print everything Meridian says first, the reply command included, then
  a line saying the rest is the message. In text mode the text goes through
  `fields.visible`: every Cc and Cf character but newline and tab is printed as its escape
  (`\x1b`, `\u202e`), so it can't clear the screen or redraw what Meridian printed above.
  `--json` carries the text as stored; JSON escapes the C0 controls itself.

## The launch line

`count.launch_line` gives a primary one guidance line at a fresh launch. It walks down
from `issued`, ages a message by its file time, skips a missing number, and stops at the
first file older than 7 days or after 200 numbers; past 50 it prints `50+`. A failure
leaves the line out. A continue omits it (`lib/launch/.context/CONTEXT.md`).

## The seam with hooks

`filing.file_from_command` calls `lib/hooks/machine.py:launch_inbox_received`, which
starts the detached process that fires `inbox.received`; the rest of `lib/inbox` imports
nothing from `lib/hooks`. That process calls `write_hook_log` and `prune_if_due` here; both never
raise.
