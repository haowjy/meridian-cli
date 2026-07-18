# Context configuration

Context paths point Meridian at directories for active work, knowledge bases, and work archives. They can be backed by a local path (default) or a remote Git repo that Meridian clones and resolves at runtime.

Configure in `meridian.toml` or `~/.meridian/config.toml`:

```toml
[context.work]
source  = "git"
remote  = "git@github.com:team/docs.git"
path    = "project/work"
archive = "project/archive/work"

[context.kb]
source = "git"
remote = "git@github.com:team/kb.git"
path   = "knowledge"

[context.strategy]
source = "git"
remote = "git@github.com:team/docs.git"
path   = "project/strategy"
```

## Schema

### `[context.work]`

| Key | Type | Default | Purpose |
| --- | ---- | ------- | ------- |
| `source` | str | `"local"` | `"local"` or `"git"` |
| `remote` | str | — | Git remote URL (required when `source = "git"`) |
| `path` | str | `"{user_home}/context/{project}/work"` | Path to the work directory (see path resolution below) |
| `archive` | str | `"{user_home}/context/{project}/archive/work"` | Path to the work archive directory |

### `[context.kb]`

| Key | Type | Default | Purpose |
| --- | ---- | ------- | ------- |
| `source` | str | `"local"` | `"local"` or `"git"` |
| `remote` | str | — | Git remote URL (required when `source = "git"`) |
| `path` | str | `"{user_home}/context/{project}/kb"` | Path to the knowledge base directory (see path resolution below) |

### `[context.NAME]`

Arbitrary named context tables are allowed alongside the built-in `work` and `kb` contexts. They support:

| Key | Type | Default | Purpose |
| --- | ---- | ------- | ------- |
| `source` | str | `"local"` | `"local"` or `"git"` |
| `remote` | str | — | Git remote URL (required when `source = "git"`) |
| `path` | str | — | Path to the context directory, relative to repo or clone root |

When `source = "git"`, Meridian clones the remote into a local cache and resolves paths relative to the clone root. Use `meridian context` to inspect the resolved paths.

### Path resolution

`path` values support two placeholders:

- `{user_home}` — the Meridian user home (e.g. `~/.meridian`).
- `{project}` — the project ID from committed `meridian.toml` `[project] id`.

For `source = "git"`, `path` is resolved relative to the clone root. For
`source = "local"`, absolute paths (including expanded placeholders) are used as
given; a relative `path` resolves against the project root.

The built-in defaults are user-scoped (`{user_home}/context/{project}/...`), so work and KB live **outside** the project repo. There is no repo-local fallback. Read-only commands with no identity create no context state; durable writes create identity before resolving these paths.
