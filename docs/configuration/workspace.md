# Workspace configuration

Workspace config expands the filesystem scope projected into harness launches. It is intentionally separate from context config:

- **Context** is working memory: named directories Meridian exposes through `MERIDIAN_CONTEXT_*_DIR` environment variables and startup prompt context.
- **Workspace** is filesystem scope: directories projected to the harness sandbox only. Workspace entries do **not** create environment variables and are not listed in the system prompt.

Workspace entries live in repo-local config only:

- `meridian.toml` — committed project conventions, such as the sibling repo layout the team expects.
- `meridian.local.toml` — local personal overrides and additions, not committed.

There is no user-global workspace config because workspace entries widen filesystem access.

## Schema

Declare one named table per root:

```toml
# meridian.toml — committed convention
[workspace.prompts]
path = "../prompts/meridian-base"

[workspace.mars]
path = "../mars-agents"

# meridian.local.toml — machine-specific override or addition
[workspace.prompts]
path = "/home/user/src/prompts/meridian-base"

[workspace.local-data]
path = "/data/large-dataset"
```

| Field | Type | Required | Purpose |
|---|---|---|---|
| `path` | str | yes | Directory to project; relative paths resolve against the project root |

Entry names must match `^[a-z][a-z0-9_-]*$`: lowercase, start with a letter, then lowercase letters, digits, hyphens, or underscores. Names are merge keys and CLI/debug identifiers; they are not exported as environment variables.

Unknown keys inside entries produce `workspace_unknown_key` findings and are ignored. Scalar values directly under `[workspace]` are invalid.

## Committed conventions and local overrides

Committed entries in `meridian.toml` describe the expected repository layout. They are conventions, not enforcement. If a committed path does not exist on a developer's machine, Meridian skips it during projection and reports a `workspace_missing_root` finding; this supports partial checkouts while still surfacing drift.

Local entries in `meridian.local.toml` are explicit machine-specific instructions. A local entry with the same name as a committed entry replaces that entry's path. Local-only entries are appended after committed entries. If a local path does not exist, Meridian skips it and emits `workspace_local_missing_root` because the local override is likely stale or mistyped.

There is no `enabled` field. There is also intentionally no subtractive override: you cannot disable a committed workspace entry while the checkout exists except by changing/removing the local filesystem path. If users need that capability, it can be added later as a backward-compatible schema extension.

## Path resolution

- Relative paths resolve against the project root (the directory containing `meridian.toml`).
- Absolute paths are used as-is after `Path.expanduser()`.
- Paths beginning with `~` are expanded with `Path.expanduser()`.
- Only paths that exist as directories are projected.

## Projection per Harness

Each existing root is projected at launch time in deterministic order: committed entries in file order, overridden entries keep their position, and local-only entries append in local-file order.

| Harness | Mechanism |
|---|---|
| Claude Code | `--add-dir <path>` flag per root |
| Codex (subprocess) | `--add-dir <path>` flag per root |
| Codex (managed primary) | `-c sandbox_workspace_write.writable_roots=[...]` on app-server launch **and** `--add-dir <path>` on `codex resume --remote` TUI attach |
| OpenCode | `OPENCODE_CONFIG_CONTENT` env with `permission.external_directory` entries; merged into any pre-existing parent config |
| Other harnesses | `unsupported:requires_config_generation` |

## Claude Code Skill Leakage

Claude Code scans every `--add-dir` path for `.claude/skills/` and loads all skills it finds. There is no way to suppress this. Workspace roots that point to repos with their own skills will pollute the session's skill namespace with irrelevant skills from those repos.

**Affected harness:** Claude Code only. Codex and OpenCode do not scan for skills.

**Mitigations:**

- **Point to parent directories** instead of individual repos. Claude Code scans `.claude/skills/` at the root of each `--add-dir` path only, not recursively. For example, `path = "../prompts"` avoids loading skills from `../prompts/some-repo/.claude/skills/`.
- **Avoid adding worktree directories.** Each worktree is a full checkout with its own `.claude/skills/`, causing duplicate skill loading.

**Known Claude Code issues:**

- [`--add-dir` loads skills but `additionalDirectories` in settings does not](https://github.com/anthropics/claude-code/issues/30064) — the two mechanisms are documented as equivalent but behave differently for skill discovery.
- [No way to disable individual skills](https://github.com/anthropics/claude-code/issues/43928) — a `disabledSkills` setting has been requested but is not implemented.

## `config show` Workspace Output

```text
workspace.status = present
workspace.sources = ["meridian.toml", "meridian.local.toml"]
workspace.roots.count = 3
workspace.roots.projected = 2
workspace.roots.skipped = 1
workspace.applicability.claude = active
workspace.applicability.codex = active
workspace.applicability.opencode = active
```

Pass `--verbose` or `--json` to get per-root detail:

```text
workspace.roots[0].name = mars-agents
workspace.roots[0].source = merged
workspace.roots[0].declared_path = ../mars-agents
workspace.roots[0].resolved_path = /home/user/repos/mars-agents
workspace.roots[0].status = projected

workspace.roots[1].name = prompts
workspace.roots[1].source = committed
workspace.roots[1].declared_path = ../prompts/meridian-base
workspace.roots[1].resolved_path = /home/user/repos/prompts/meridian-base
workspace.roots[1].status = skipped (path not found)

workspace.roots[2].name = local-data
workspace.roots[2].source = local
workspace.roots[2].declared_path = /data/large-dataset
workspace.roots[2].resolved_path = /data/large-dataset
workspace.roots[2].status = projected
```

`source` values: `committed` (from `meridian.toml` only), `local` (from `meridian.local.toml` only), `merged` (local path overrides a committed entry). A `skipped` root contributes to `workspace.roots.skipped` but is not projected to harness launches.

Status values: `none` (no workspace entries), `present` (parsed OK), `invalid` (parse or schema error). Workspace findings, when present, render as separate `warning:` lines.
