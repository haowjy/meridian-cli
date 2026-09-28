# Model catalog commands

`meridian mars models list` is a **human display** of curated harness–model
possibilities. It is not a machine-readable alias or raw-catalog API. Use
`--all` to include hidden rows and `--live` to add current fixed-harness
eligibility; eligibility is not a launch decision.

Use `meridian mars models aliases --json` for the static alias inventory. An
alias's `harness` is an authored preference, not proof of an available route.
Use `meridian mars models resolve ALIAS --json` for per-alias resolution; the
launch bundle makes the final runtime routing decision.

Use `meridian mars models catalog --json` for the raw models.dev cache. Its
`catalog` entries include model IDs, provider, release date, description,
context/output limits, and costs. They do not include harness routes,
matched aliases, or live availability. Meridian's catalog listing projects
these raw entries without inventing missing routing fields. The
`meridian.models.list` extension accepts only `project_root` and returns
`models` with `model_id`, provider, description, release date, context/output
limits, costs, and a derived cost tier when available. It has no harness,
alias, name, family, capabilities, or pinned fields.

Use `meridian mars models refresh` to force a models.dev cache refresh.
`meridian doctor` checks harness installation and health.
