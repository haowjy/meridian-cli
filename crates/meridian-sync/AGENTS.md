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
  a timeout sends SIGTERM to git's process group and keeps waiting. Every call is confined
  by `GIT_CEILING_DIRECTORIES`, so a clone path that lost its `.git` can't resolve to a
  repository above it.
- `pass`: `run_pass` and its outcome and reason enums. Integration happens off the worktree
  (`merge-tree`, `commit-tree`, `merge --ff-only`), and only the fast-forward writes the
  worktree. A pass holds `<home>/locks/clone-<key>.lock` (flock, same file as the Python
  `git-autosync` hook) until C deletes the hook; delete `CloneLock` with it.
- `records`: reads `<home>/autosync/roots/*.json`, which Python writes.
- `tests/pass.rs` runs real git against a bare remote in a temp dir. A test that edits the
  clone mid-pass does it from the `StepEvent` observer, which runs on the pass's thread
  right after a git call starts.
- The cargo target dir is per checkout (about 230 MB with tests). To share one across
  worktrees, export `CARGO_TARGET_DIR` in your shell; `uv sync` honors it.
