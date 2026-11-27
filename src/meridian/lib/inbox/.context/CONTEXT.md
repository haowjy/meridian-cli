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
session itself, and the receiving side of a machine call (lane 2) passes its own. The CLI
only checks the caller's role, parses and prints. `offer.taker_running` holds the rule for
whether a chat's run is live, which `list --taken` shows; `cli/message.py` has its own
copy until the machine-wide-IDs rewrite of that file lands.

**Prune** (`prune.py`): scans without the lock and takes it for each deletion, checking
again under it. A message taken more than 30 days ago is deleted in this order: message,
`by-origin` link, offer record, hook log, claim. The claim goes last so a kill part way
leaves records with no message, which the next pass removes, and never a message that
looks waiting again. A waiting message is never deleted. It runs at most once a day (the
`pruned` stamp), from the detached hook process and from every `meridian inbox` command;
not at a primary launch, because the machine that runs a chat bot launches none.

## Who is calling

`caller.resolve_caller` is the one answer for all six commands. It takes the invocation's
`Caller` (`get_global_options().caller`), which survives `-C`; `MERIDIAN_SPAWN_ID` read
from the environment does not, and a child running `meridian -C ../other inbox take`
would pass for a terminal. An ID with no spawn record is refused, never treated as a
terminal. The session's chat ID comes from its record: `MERIDIAN_CHAT_ID` names the chat
that launched a child.

- A **terminal** (no session: a person, a daemon) sees everything and may do everything.
- A **primary** (`kind == "primary"`) may `take`, `release`, `offer` and pass `--all`.
- Any other session, `streaming` included, may `list` and `read`.

This stops a confused child, not one that forges its environment.

`caller.project_of_root` is the one definition of a session's project: the path of the
runtime root its record was found in, below `projects/` (an ID may contain `/`). The
caller, `offer`'s check on the receiving session and the launch line all use it. An ad-hoc
project (`adhoc.…`) counts as none. A session sees messages with its own project or with
none. `meridian.toml` can set any string as a project ID, and `_MERIDIAN_RUNTIME_DIR` can
put a root anywhere, so a root that gives no ID inside `fields.SAFE_FIELD` refuses the
command: read as "no project", that session's messages would be shown to every session.

A process no session runs has a project only from `--project-id`, never from its working
directory: a hook row runs in home, and a daemon's directory is not what its messages are
about.

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
