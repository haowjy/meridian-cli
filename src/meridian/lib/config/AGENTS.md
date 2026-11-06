# Configuration

Use `mutate_project_config()` for `meridian.toml`: it preserves comments and
serializes the entire read-modify-write, including identity creation.

A field without `config_field()` metadata validates from TOML but has no generic
config command or env override. Hook rows have their own validation boundary.

[.context/CONTEXT.md](.context/CONTEXT.md) explains the exceptions to merging.
