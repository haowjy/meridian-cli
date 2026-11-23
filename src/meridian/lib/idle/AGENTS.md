# Idle policy

Adapters report facts; idle core decides. Classify background work in `wake.py`
so only work that will wake the session holds a push. Permission notices are
stateless and do not alter an idle stretch.

A primary reopen closes its stretch before exec: exit hooks may not run after a
terminal is killed, and adapters cannot distinguish reload from resume. TTL
lookup uses the launch-recorded native store, not the current directory, which
may have changed.

The sidecar runs inside the TUI (or, for agy, in the plugin's MCP server, which is not
the terminal's process): contain sensor failures and keep diagnostics away from the
terminal. [.context/CONTEXT.md](.context/CONTEXT.md) describes
wake and reopen behavior.

To test an idle stretch live, launch the TUI with `MERIDIAN_NOTIFY_PUSH_BACKEND=none`
and `MERIDIAN_NOTIFY_EMAIL_BACKEND=none`. Every interactive session counts as
primary, including one an agent starts in a private tmux, so otherwise its push and
cache warning reach Jimmy's phone and inbox.
