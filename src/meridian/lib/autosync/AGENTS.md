# lib/autosync

Python's half of context autosync; the daemon is `crates/meridian-sync`. The seam is two
files under `<home>/autosync/`: Python writes `roots/<sha16(root)>.json` (one per project
checkout, `state.register_project`, called once from `cli/main.py`'s `_autosync_at_startup`)
and reads `state.json`, `daemon.lock` and `service.off`. Python reads no clock for any
state decision. The daemon's own AGENTS.md has the other side.

- `state.py`: the record write, the lock probe, `state.json` as read, `clone_syncs` (one row
  per clone: the one decision about it, with `autosync` on when any context naming it has
  it). Surfaces (`meridian context`, `sync status`, `doctor`, the launch line) all go through
  `ops/sync_daemon.py`, which only renders those rows: add no per-context decision there.
- `service.py`: `ensure_service`, the only code that writes or starts the unit or plist,
  through one manager object per platform. Rewrite the file only for an *older* template or
  a missing binary: a unit from a newer template, or naming another install's live binary,
  is left alone, so a uv tool and a project venv don't fight. Restart the service whenever
  the lock probe finds no daemon: that one rule repairs a deleted `autosync/`, a stopped
  service and a moved install. Bump `TEMPLATE_V` when the unit text changes.
- The default home and the unit's directory come from the passwd home, never `$HOME`: the
  user service manager ignores `$HOME`, so a test with a temporary `HOME` would otherwise act
  on the real unit.
- Auto-install (`auto=True`, the launch) skips agent sessions, dev installs, a non-default
  home and `service.off`; the launch path must stay cheap when the daemon runs (one file
  read, one lock probe, no subprocess), must never raise, and never waits for a daemon it
  just started (its `systemctl` calls time out in seconds). `sync service install` is the
  explicit path for everything else.
- A non-default home gets its own unit name (`meridian-sync-<sha8(home)>`), so a test with a
  temporary `MERIDIAN_HOME` never touches the real service.
- `clones.py`: the inside-a-clone predicate that refuses spawns and projects in a clone.
