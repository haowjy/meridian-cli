# meridian-sync

A Rust binary shipped as its own wheel, released at meridian-cli's version and pinned
exactly by it. Design: the context-autosync design's "Packaging and release".

- Gate (CI runs it): `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`,
  `cargo test --locked`.
- The version lives in four files, listed once in `src/meridian/dev/versions.py`; never edit
  one by hand. `uv run python -m meridian.dev.lints` fails when they disagree.
- `[tool.uv] cache-keys` in `pyproject.toml` must name every file the build reads, or
  `uv sync` ignores an edit to it.

## Engine (`src/lib.rs`)

- `git`: the one place git is spawned. Never SIGKILL (a killed `git add` leaves `index.lock`):
  a timeout sends SIGTERM then SIGCONT (a stopped process holds SIGTERM pending) to git's
  process group and keeps waiting for git's own pid, but never for a grandchild holding the
  output pipes: a short grace after git exits, then they are dropped. Extra environment goes
  in `PassEnv::git_env`; tests never call `set_var`. Cancel is an end state: once
  `PassEnv::cancel` is set no git starts, and a call it killed is an `Interrupted` error, so a
  pass ends instead of acting on a killed git's answer. Every call is confined
  by `GIT_CEILING_DIRECTORIES`, so a clone path that lost its `.git` can't resolve to a
  repository above it.
- `pass`: `run_pass` and its outcome and reason enums. Integration happens off the worktree
  (`merge-tree`, `commit-tree`, `merge --ff-only`), and only the fast-forward writes the
  worktree. Only a clone's one worker runs passes on it, inside the one process holding
  `daemon.lock`; there is no clone lock.
- `conflict`: `SYNC-CONFLICT.md`. A worker writes it into each context directory of a
  conflicted clone and deletes it after a pass that ends `synced` (a failed fetch changes
  nothing). `pass` excludes it from `git add` and `status` by pathspec, because the docs
  clone's allowlist `.gitignore` re-includes `*.md` over any exclude file; `watch` ignores it.
- `records`: reads `<home>/autosync/roots/*.json`, which Python writes, and groups contexts
  by `clone_key` (its `state.json` entry): records disagreeing on the path for a key lose to the
  newest and are listed in `skipped`.
- `watch`: `notify` plus the `ignore` crate; a hint only, `git status` inside the pass decides.
  Linux takes one non-recursive watch per unignored directory (over 20,000, or the kernel
  limit, falls back to polling and retries every 5 minutes); every directory, first walk or
  new, is judged by a walk from the clone's root, so one ignore decision applies; macOS takes one recursive stream and filters with the root
  ignore files, which is untested here.
- `state`: `state.json`'s model, its atomic write, and `Tracker`, the judge. Every duration
  is an `Instant` passed in, so the stopped rules are unit-tested with an injected clock.
  A tracker restored mid-pass doesn't count the restarted worker's steps as progress until a
  pass finishes: that is what lets a restart loop around a hung git still reach the
  15-minute stop.
- `daemon`: the lock, one worker thread per clone, and the main thread, the only writer of
  `state.json`. When a clone becomes stopped (once; a restored stop doesn't count) it runs
  `meridian -C <newest record's root> notify` from beside its own binary, never waiting:
  each loop checks the child, and one past 60 s gets SIGTERM. At start the daemon drops every
  `MERIDIAN_*` (except `MERIDIAN_HOME`) and `_MERIDIAN_*` variable, so only file-configured
  notify settings reach it. Shutdown cancels git through `PassEnv::cancel` (SIGTERM, as at a timeout)
  rather than signalling pids. Exit 0 stops for good (home gone, or the binary missing past a
  5 s grace); 75 asks for a restart (binary replaced, or `daemon.lock` no longer the file the
  daemon holds). `tests/daemon_exit.rs` runs these through the binary.
- `meridian-sync version` prints Python's spelling (`0.24.0rc1`); `state.json`'s `version`
  and `meridian sync status` use it too.
- `tests/pass.rs` runs real git against a bare remote in a temp dir. A test that edits the
  clone mid-pass does it from the `StepEvent` observer, which runs on the pass's thread
  right after a git call starts.
- The cargo target dir is per checkout (about 230 MB with tests). To share one across
  worktrees, export `CARGO_TARGET_DIR` in your shell; `uv sync` honors it.
