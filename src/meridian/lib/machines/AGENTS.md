# Machines

Calls to other machines over plain SSH. Design: `machines.md` in the machine-wide-ids work item.

- `call.call_machine` is the only way to call: it prints nothing and returns `NotSent`,
  `Unanswered` or `Answered`. The split is the `meridian-ready` line: before it nothing was
  sent (safe to run again); after the first request byte, any outcome without a whole
  `meridian-result` line is `Unanswered`, one `try` with no exception let out.
- Nothing the user typed goes on the remote command line: it is always the cached absolute
  path, `machine`, `run`, matching `SAFE_PATH_RE`. The request and the discovery script go on
  stdin. A user-typed target goes to `ssh` only after `ssh_target_problem`, behind `--`.
- Meridian stops only an `ssh` or child it started, through its handle. `machine run` ends its
  child at `limit` and when its stdin reaches EOF: without a terminal, killing the caller's
  `ssh` leaves the remote command running.
- The ready line is data from the other machine: check name, ID and `v` before using them.
- The time limits in `call.py` are placeholders until probe G sets them; keep them together.
- No connection is reused (no `ControlMaster`): a user may set it in `~/.ssh/config`.
