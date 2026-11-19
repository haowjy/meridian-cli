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
| `autosync` | bool | see below | Keep the clone in sync with its remote |
| `path` | str | `"{user_home}/context/{project}/work"` | Path to the work directory (see path resolution below) |
| `archive` | str | `"{user_home}/context/{project}/archive/work"` | Path to the work archive directory |

### `[context.kb]`

| Key | Type | Default | Purpose |
| --- | ---- | ------- | ------- |
| `source` | str | `"local"` | `"local"` or `"git"` |
| `remote` | str | — | Git remote URL (required when `source = "git"`) |
| `autosync` | bool | see below | Keep the clone in sync with its remote |
| `path` | str | `"{user_home}/context/{project}/kb"` | Path to the knowledge base directory (see path resolution below) |

### `[context.NAME]`

Arbitrary named context tables are allowed alongside the built-in `work` and `kb` contexts. They support:

| Key | Type | Default | Purpose |
| --- | ---- | ------- | ------- |
| `source` | str | `"local"` | `"local"` or `"git"` |
| `remote` | str | — | Git remote URL (required when `source = "git"`) |
| `autosync` | bool | see below | Keep the clone in sync with its remote |
| `path` | str | — | Path to the context directory, relative to repo or clone root |

When `source = "git"`, Meridian clones the remote into a local cache and resolves paths relative to the clone root. Use `meridian context` to inspect the resolved paths.

### `autosync`

Every git context takes `autosync`. The `meridian-sync` daemon commits edits in the clone,
fetches, merges and pushes; see [Context Sync](../commands.md#context-sync).

- **Default:** on for a clone Meridian manages (under `~/.meridian/git/`), off when a
  `[git."<url>"].path` override points the clone at a checkout you manage. Setting it to
  `true` on an override opts that checkout in; the daemon then keeps it on the remote's
  default branch.
- **A clone syncs when any context naming it has `autosync` on.** Contexts in several
  projects can share one clone. To stop syncing one on this machine, set `autosync = false`
  in `meridian.local.toml` for each project that names it.
- `autosync = true` on `source = "local"`, or with no `remote`, is a config error.
- **Machine-wide off:** `meridian sync service uninstall`.
- **The service needs an ssh agent the user service manager can see.** It starts outside
  your shell, so a `SSH_AUTH_SOCK` set only in a login shell isn't there. Where the clone's
  remote uses ssh and the agent is missing, `meridian sync status` shows `fetch_failed` with
  git's `Permission denied (publickey)`, and after 15 minutes the clone stops. Fix it by
  making the agent known to the manager (`systemctl --user import-environment
  SSH_AUTH_SOCK`, or a socket-activated agent) and run `meridian sync now`; or use an https
  remote with a credential helper.

### Path resolution

`path` values support two placeholders:

- `{user_home}` — the Meridian user home (e.g. `~/.meridian`).
- `{project}` — the project ID from committed `meridian.toml` `[project] id`.

For `source = "git"`, `path` is resolved relative to the clone root. For
`source = "local"`, absolute paths (including expanded placeholders) are used as
given; a relative `path` resolves against the project root.

The built-in defaults are user-scoped (`{user_home}/context/{project}/...`), so work and KB live **outside** the project repo. There is no repo-local fallback. Read-only commands with no identity create no context state; durable writes create identity before resolving these paths.
