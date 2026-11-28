# Configuration

Precedence: CLI flags > env vars > YAML profile > project config > user config > harness
default. A derived field takes the precedence of its source. One exception: the model,
harness and effort an agent's user-only section sets (`agent_routing.py`, `[inbox.router]`)
rank below env and above that agent's profile and Mars overlay.

A section in `USER_ONLY_SECTIONS` is read from `~/.meridian/config.toml` only, by its
owner at use time, never through `MeridianConfig`; other files ignore it with a warning.

Use `mutate_project_config()` for `meridian.toml`: it preserves comments and
serializes the entire read-modify-write, including identity creation.

A field without `config_field()` metadata validates from TOML but has no generic
config command or env override. Hook rows have their own validation boundary.

[.context/CONTEXT.md](.context/CONTEXT.md) explains the exceptions to merging.
