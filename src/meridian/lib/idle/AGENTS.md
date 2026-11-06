# Idle policy

Adapters report facts; idle core decides. Classify background work in `wake.py`
so only work that will wake the session holds a push. Permission notices are
stateless and do not alter an idle stretch.

A primary reopen closes its stretch before exec: exit hooks may not run after a
terminal is killed, and adapters cannot distinguish reload from resume. TTL
lookup uses the launch-recorded native store, not the current directory, which
may have changed.

The sidecar runs inside the TUI: contain sensor failures and keep diagnostics
away from its terminal. [.context/CONTEXT.md](.context/CONTEXT.md) describes
wake and reopen behavior.
