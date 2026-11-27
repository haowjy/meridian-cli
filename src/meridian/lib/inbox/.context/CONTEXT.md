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
program and sender) succeeds and anyone else is refused. The machine is compared by ID
(`Party.same_as`, the one rule for whose a claim is): `machine` is the name at the time,
kept as a label and for the reply address, and a rename must not lose a session its claim.
A party written before `machine_id` was stored has only the name, and is this machine's
when that is its current or a former name. **Release** unlinks the claim; a
message with no claim is already released, which is a success so a cut-off call can be
repeated.

**Offer** (`offer.offer`): under the lock check the message exists and is not taken;
release the lock; find the session and refuse one that is not a primary (an offer says
"claim it", and only a primary may take) or is in another project than the message's; send
the notice; take the lock again, check once more, and append to `offered/<id>.json`. Send
first, record second: a crash between the two leaves a session that was told and no
record, and the repeat tells it again. The lock is never held across the lookup or the
send: for a session on another machine each is a call there. Finding the session and
sending are functions the caller passes in. The CLI passes `offer.local_target` (the spawn
store) or, for `<machine>/<id>`, a lookup that asks that machine, and
`cli/message.py:send_message`, which refuses a cold or stopped session itself, on this
machine or through the other machine's own `message`. `ops/locate.run_is_live` is the one
rule for whether a chat's run is live; `message` and `list --taken`
(`offer.taker_running`) both use it.

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

`caller.project_of_root` is the one definition of a session's project in the inbox: what
`state/runtime_root.runtime_root_project_id` gives for the runtime root its record was
found in, which is the root's whole path below `projects/` (`team/app`). The
caller, `offer`'s check on the receiving session and the launch line all use it. An ad-hoc
project (`adhoc.…`) counts as none. A session sees messages with its own project or with
none.

A root that gives no ID `fields.is_project_id` accepts refuses the command. An ID is in
the safe set and has the form `core/refs.is_project_id_form` checks (parts joined by `/`,
none empty, `.` or `..`), the one predicate `lib/machines/caller.py` also holds a caller
block's project ID to. Two things produce a
root that fails: `meridian.toml` can set any string as a project ID, and
`_MERIDIAN_RUNTIME_DIR` can put a root anywhere. The command is refused because the
alternative is to read such a session as having no project, and its messages would then
be shown to every session.

A process no session runs has a project only from `--project-id`, never from its working
directory: a hook row runs in home, and a daemon's directory is not what its messages are
about.

## Other machines

Each machine has its own inbox, and a command reaches another machine's through step 4's
one call (`lib/machines/`): the other machine's own `meridian` runs the same command on its
own files. Nothing here reads or writes another machine's directory.

- **Filing.** `<machine>/inbox` is a reference `core/refs.parse_ref` knows (kind `inbox`),
  so `machine_forward.other_machine` decides which machine it names, as for every
  reference. `cli/message.py` sends `message inbox - --json` with the five flags; the
  answer comes back as `Filed` with that machine's name.
- **The five commands** are on the crossing list (`CommandDescriptor.crosses`) and take
  `--machine <name>`. A message ID is always bare (`m412`): `<machine>/m412` is not a
  reference, because `parse_ref`'s machine form is for session IDs. Each handler checks
  the role, then calls `forward_if_remote`. `take`, `release` and `offer` pass `may_have`:
  a call that was sent and got no whole answer fails with kind `maybe_delivered`, not
  `unreachable`, because it may have acted.
- **The role is checked on the calling side, before it connects.** The request carries
  who is calling and no role. So `InboxCaller.may_change` is true for every caller from
  another machine, and a build that crosses must keep `_require_changer` ahead of
  `_cross` in every handler.
- **Who is calling, on the receiving side,** is `machines/caller.MachineCaller`
  (`caller._from_machine`): the session's chat ID (`reply_id`) and project
  (`project_id`), or with no session a terminal of that machine. Its `machine_id` is the
  request's, which is what tells its sessions from this machine's; the name beside it is
  the one this machine saved it under, else the one it gives itself. Its reply address
  is `message/reply.way_back`'s answer, the rule the reply line of a delivered message
  uses: none when this machine hasn't saved the caller's, or the session can't be reached.
  A block whose session ID is not an ID never arrives: `machine run` refuses it, because
  a block with no session is a terminal, which may do more.
- **What the calling side sends** is built from the `InboxCaller` it resolved and
  checked (`cli/message.py:inbox_caller_block`, from the runtime root the record was
  found under), through step 4's `session_caller_block`. Don't build it from the
  environment: that is a second resolver, and the two disagreed for a session whose
  working directory was in another project. A session with no record or no usable
  project is refused before anything connects.
- **A repeat is safe**, which is what makes `maybe_delivered` recoverable: the taker is
  (machine, chat ID), the same on every retry, so taking again succeeds; releasing a
  message with no claim succeeds; a repeated offer sends the notice again; and a filing
  with a `via` ref matches `by-origin`. A filing without one can be filed twice.
- **`offer` runs where the message is.** Its session may be on another machine
  (`<machine>/<id>`): the notice then goes out as an ordinary cross-machine `message`,
  names `m412 on <this machine>` and gives `meridian inbox take m412 --machine <this machine>`
  on a line of its own, unquoted (Claude's delivery drops backticks).
  What that machine reports decides the refusals: `spawn show --json` gives the kind (it
  leaves a child's out, so "no kind" is "not a primary"); `spawn list --global --json`
  gives the project ID and is asked only when the message has a project, and a session
  missing from it is not running; whether it runs and whether it is cold are that
  machine's `message` to refuse. Those two reads go through
  `machine_forward.ask_machine`, the one "ask another machine a read" beside
  `forward_if_remote`; `_remote_target` only checks fields, and keeps a `kind` only when
  this build knows it. A command another machine sent can't name a third
  machine (`other_machine`), so `offer <id> <machine>/<id> --machine <holder>` is refused
  there: run `offer` on the machine that holds the message.
- **The hook process drops `_MERIDIAN_MACHINE_CALL`** (`session_free_env`: it is an
  injected handle), so an `inbox.received` row started by a filing from another machine
  runs as this machine, and its own `offer` may cross.
- **Older peers** answer with a kind, never a traceback: a build without step 4 is
  `version`; one without the crossing list entries is `refused` ("doesn't reach another
  machine"); for `message <machine>/inbox`, lane 1's build answers `refused` and a build
  with no inbox `usage`.

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
