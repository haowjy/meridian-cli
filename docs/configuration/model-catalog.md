# Model catalog configuration

`meridian mars models list` shows the static alias/catalog inventory for the
current project. Use `--all` to include all alias candidates or `--catalog` for
the raw models.dev cache.

Use `meridian mars models list --live` when you need routed availability,
selected harnesses, and runnable paths. In live mode, `--unavailable` includes
unavailable routes.

Use `meridian mars models resolve ALIAS --json` for the authoritative mapping a
spawn will use in the current project and local config.

Builtin aliases (`opus`, `sonnet`, `haiku`, `codex`, `gpt`, `gemini`) auto-resolve to the latest model per family. The default list filters aggressively:

- Date-suffixed variants hidden when base model exists
- Superseded models hidden when a newer lineage successor exists
- Models older than ~120 days hidden by default
- High-cost models (≥$10/M input tokens) hidden by default

Use `meridian mars models refresh` to force a cache refresh from the models.dev catalog.

In `models list --live` output, Cursor models appear as `runnable` when the
`cursor` binary is installed and the harness probe succeeds. If `cursor` is not
on `PATH`, Cursor routes show as `unavailable`. Run `meridian doctor` to check
harness status.
