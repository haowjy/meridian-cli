# Machines

Calls to other machines over plain SSH.

- `call.call_machine` is the only way to call: it prints nothing and returns `NotSent`,
  `Unanswered` or `Answered`. The split is the `meridian-ready` line: before it nothing was
  sent (safe to run again); after the first request byte, any outcome without a whole
  `meridian-result` line is `Unanswered`, one `try` with no exception let out.
- An `ssh` that exits 255 before the ready line gets its kind and text from
  `ssh.not_connected`, for the call and for discovery. It is the one place that reports a
  caller inside its session's sandbox as `sandboxed`; don't name the kind elsewhere.
- Nothing the user typed goes on the remote command line: it is always the cached absolute
  path, `machine`, `run`, matching `SAFE_PATH_RE`. The request and the discovery script go on
  stdin. A user-typed target goes to `ssh` only after `ssh_target_problem`, behind `--`.
- Meridian stops only an `ssh` or child it started, through its handle. `machine run` ends its
  child's process group at `limit` and when its stdin reaches EOF: without a terminal, killing
  the caller's `ssh` leaves the remote command running.
- A command that changes state on the other machine passes `forward_if_remote` a `may_have`
  sentence: its `Unanswered` is kind `maybe_delivered`, and only a command whose repeat is
  safe may cross that way. The sentence ends with whether to run it again; where a repeat
  isn't safe for some caller, it says what to check first.
- `machine run` puts a directory holding only `meridian` first on its child's `PATH`, never
  the install's script directory, whose `python3` would shadow the system's in hook rows.
- `forward_if_remote` and `message` both ask `machine_forward.other_machine` which other
  machine a command names; it is the one place that refuses two machines, `-C` applied here,
  and a third machine from a call another machine started. Don't decide it elsewhere.
  Only `message` sends `<machine>/inbox` on: `forward_if_remote` refuses it as `usage`.
- Way back (`cli/machine_back.py`): candidate targets are tested from the other machine over
  `/bin/sh -s` (script and candidates on stdin, none on the command line), then the other side
  runs `<path> machine add <target>` and the saved list is read again to say what happened.
  With no candidate answering there is no default.
- A write under the home that the call doesn't need (the path cache, a rename correction) goes
  through `home_write.best_effort`: a failure is a warning.
- The ready line is data from the other machine: check name, ID and `v` before using them.
- Keep the time limits together in `call.py`. Measured, none is tight: a new connection
  takes about 0.2 s and a read about 0.6 s.
- No connection is reused (no `ControlMaster`): a user may set it in `~/.ssh/config`.
