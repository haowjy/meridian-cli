# Getting Started

## Prerequisites

Meridian is a coordination layer — it needs at least one harness installed to run agents.

| Harness     | Model prefixes                  | Install                                                              |
| ----------- | ------------------------------- | -------------------------------------------------------------------- |
| Claude Code | `claude-*`, `sonnet*`, `opus*`  | [docs.anthropic.com](https://docs.anthropic.com/en/docs/claude-code) |
| Codex CLI   | `gpt-*`, `codex*`, `o3*`, `o4*` | [github.com/openai/codex](https://github.com/openai/codex) |
| OpenCode    | anything else                   | [opencode.ai](https://opencode.ai) |

**Claude Code** is the most mature primary-session harness. Codex also supports managed primary TUI passthrough, including hidden instruction routing and managed session tracking. See [codex-tui-passthrough.md](codex-tui-passthrough.md).

**Platform**: macOS, Linux, Windows, WSL.

## Performance Tip: Keep Prompt Cache Warm

Long-running spawns can lose prompt-cache warmth while Meridian waits for them to finish. When the cache goes cold, the harness reprocesses the full context on resume — costing extra tokens and time.

| Harness     | Default cache TTL | How to extend |
| ----------- | ----------------- | ------------- |
| Claude Code | 5 minutes (default) or 1 hour (subscription) | Set `ENABLE_PROMPT_CACHING_1H=1` in `~/.claude/settings.json` |
| Codex CLI   | 5–10 minutes typical | Extended cache up to 1 hour (automatic on most models) |

**For Claude Code users**, create or edit `~/.claude/settings.json`:

```json
{
  "env": {
    "ENABLE_PROMPT_CACHING_1H": "1"
  }
}
```

This opts into the 1-hour prompt-cache TTL so Meridian's default 50-minute `spawn wait` yield interval stays comfortably inside the warm window.

If you cannot extend cache TTL, lower Meridian's yield interval instead:

```bash
# One-off adjustment for a single wait
meridian spawn wait --yield-after-secs 240

# Global override via environment variable
export MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS=240
```


## Install

```bash
uv tool install meridian-cli
```

If `meridian` is not found after install, run `uv tool update-shell` and restart your shell.

## Initialize a Project

```bash
cd your-repo
meridian init
```

This creates `.meridian/` with a project UUID and default `.gitignore`. The `.gitignore` inside `.meridian/` is managed automatically — it tracks `id`, `kb/`, `work/`, and `archive/` and ignores everything else. Spawn history and session state live at the user level (`~/.meridian/projects/<uuid>/`) and are never committed.

Bare `meridian init` bootstraps Meridian config/runtime only. It does not initialize Mars packages on its own.

## Tool Integration

To initialize Mars and install packages from the top-level command, use `--add`:

```bash
meridian init --add haowjy/meridian-dev-workflow --link .claude
```

`meridian init --add ...` initializes Mars when needed, installs packages, and applies requested links.

For first-run onboarding, `bootstrap` can do setup and launch the guided primary session in one command:

```bash
meridian bootstrap --add haowjy/meridian-dev-workflow --link .claude
```

Setup flags (`--add`, `--link`) cannot be combined with `--dry-run`.

To expose already-installed package content to a harness tool directory (`.claude/`, `.cursor/`, etc.), use the top-level convenience flag:

```bash
meridian init --link .claude
```

`meridian init --link` keeps config/bootstrap at the top level and delegates package-link wiring to mars:
- no `mars.toml` yet: runs `meridian mars init --link .claude`
- existing mars project: runs `meridian mars link .claude`

You can also call mars directly:

```bash
meridian mars init --link .claude
```

After `mars.toml` exists, additional link targets use:

```bash
meridian mars link .claude
```

This links the mars-compiled output into the target harness directory so skills are discoverable. Skills are compiled from `.mars/skills/` into the harness-native directory (e.g. `.claude/skills/`).

## Verify Setup

```bash
meridian config show   # confirm resolved config
meridian mars models list   # confirm available models
meridian doctor        # check harness connectivity
```

## Multi-Repo Workspace (optional)

If you work across multiple repos and want agents to access sibling directories, add workspace entries. Projects may commit shared conventions in `meridian.toml`; put machine-specific path overrides and local-only roots in `meridian.local.toml`.

```bash
meridian workspace init   # creates or updates meridian.local.toml with [workspace] examples
```

Edit `meridian.local.toml` if your checkout differs from the committed convention or you need an additional local root:

```toml
[workspace.sibling]
path = "../sibling-repo"
```

Existing roots are projected to harness launches automatically — `--add-dir` for Claude Code and Codex, `OPENCODE_CONFIG_CONTENT` for OpenCode. See [configuration.md](configuration.md#workspace) for full schema.

## Next Steps

- [commands.md](commands.md) — full CLI reference
- [configuration.md](configuration.md) — config keys, model routing, environment variables
- [agent-profiles.md](agent-profiles.md) — agent profile format, `model-policies`, `no-fallback`, and `mode`
  (legacy `fanout` and `fallback-order` profiles must be migrated to `model-policies` list order + `no-fallback: true`)
- [codex-tui-passthrough.md](codex-tui-passthrough.md) — managed Codex startup, bootstrap, and attach behavior
- [hooks.md](hooks.md) — hook events, builtin hooks, and `git-autosync`
- [plugin-api.md](plugin-api.md) — stable API for hook and plugin authors
