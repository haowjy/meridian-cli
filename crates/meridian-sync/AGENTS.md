# meridian-sync

A Rust binary shipped as its own wheel, released at meridian-cli's version and pinned
exactly by it. Design: the context-autosync design's "Packaging and release".

- Gate (CI runs it): `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`,
  `cargo test --locked`.
- The version lives in four files, listed once in `src/meridian/dev/versions.py`; never edit
  one by hand. `uv run python -m meridian.dev.lints` fails when they disagree.
- `[tool.uv] cache-keys` in `pyproject.toml` must name every file the build reads, or
  `uv sync` ignores an edit to it.
