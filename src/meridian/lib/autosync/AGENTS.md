# lib/autosync

Python's half of context autosync; the daemon is `crates/meridian-sync`. The seam is two
files under `<home>/autosync/`: Python writes `roots/<sha16(root)>.json` (one per project
checkout, `state.register_project`, called once from `cli/main.py`'s `_autosync_at_startup`)
and reads `state.json`, `daemon.lock` and `service.off`. Python reads no clock for any
state decision. The daemon's own AGENTS.md has the other side.

- `state.py`: the record write, the lock probe, `state.json` as read, `context_sync` (what a
  surface shows for each context). Surfaces (`meridian context`, `sync status`, `doctor`,
  the launch line) all go through `ops/sync_daemon.py`, which renders `context_sync`.
- `service.py`: `ensure_service`, the only code that writes or starts the unit or plist.
  Rewrite the file only for a new `TEMPLATE_V` or a missing binary: a unit naming another
  install's live binary is left alone, so a uv tool and a project venv don't fight. Restart
  the service whenever the lock probe finds no daemon: that one rule repairs a deleted
  `autosync/`, a stopped service and a moved install. Bump `TEMPLATE_V` when the unit text
  changes.
- Auto-install (`auto=True`, the launch) skips agent sessions, dev installs, a non-default
  home and `service.off`; the launch path must stay cheap when the daemon runs (one file
  read, one lock probe, no subprocess) and must never raise. `sync service install` is the
  explicit path for everything else.
- A non-default home gets its own unit name (`meridian-sync-<sha8(home)>`), so a test with a
  temporary `MERIDIAN_HOME` never touches the real service.
- `clones.py`: the inside-a-clone predicate that refuses spawns and projects in a clone.
