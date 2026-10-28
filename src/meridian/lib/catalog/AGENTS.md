# lib/catalog/

Translates model aliases and agent profile names into execution parameters before any
process launches. Mars resolves skills as part of the launch bundle; Meridian has no
filesystem skill catalog. Mars subprocess integration, resolution pipeline, and the
`.mars/models-merged.json` fallback: `.context/CONTEXT.md`.

## Rules

- **Read what Mars wrote, never Mars's config files.** Parsing `mars.toml` misses
  `mars.local.toml` and the user `config.toml`. Routing and per-launch policy come from the
  launch bundle; aliases from `mars models aliases --json` (else `.mars/models-merged.json`);
  which harnesses got native agents from `.mars/native-agents.json` (`agent_copy.py`).
  Checking that a `mars.toml` exists to find the root is fine. When Mars exposes no output
  for a fact, add one in mars-agents (`../mars-agents`) and bump Meridian's bundled
  `mars-agents` pin, since Meridian runs the Mars installed beside it.
- **Zero builtin aliases.** Every alias → model-id mapping comes from mars packages
  (`.mars/models-merged.json`); never hardcode one here.
- **`mars_roots.py` is the only package-root decider.** Project content follows Mars's
  nearest-`mars.toml` walk-up; otherwise the primary root is the user package project at
  `~/.meridian/mars`. Every Mars subprocess that touches package content gets the
  resolved primary as `--root`; the primary stays the sole configuration authority.
  `--fallback-root` (user content in project scope) is passed only when
  `mars_capabilities.py` confirms the installed Mars supports it. Exceptions:
  `meridian mars init` / `init --link` target the literal project root (so a nested Mars
  project can be created in a subdirectory), and the `--help` capability probe takes no root.
- **`ensure_user_mars_root()` only on operations that need a runnable root.** Read-pure
  diagnostics (bare-directory `doctor`) report the resolved path without initializing it.
- **`CatalogSession` is one launch operation, then discarded.** Its `MarsResultCache`
  is per-instance: a module-level cache would outlive the CLI invocation and block alias
  updates.
- **`run_mars_models_resolve` raises when mars is unavailable; alias/catalog inventory
  queries return `None`.** Resolution is a hard error, inventory degrades. No soft
  fallback on resolve.
- **`AliasEntry.harness` raises if absent; use it only when a harness is required.**
  Static aliases may have no authored harness and catalog exact-ID matches have no route.
  Never guess from model-name patterns; routing comes from the launch bundle.

Start at `catalog_session.py`.
