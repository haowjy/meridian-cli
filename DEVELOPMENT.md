# Development

## Setup

```bash
git clone https://github.com/meridian-flow/meridian-cli.git
cd meridian-cli
uv sync --extra dev
scripts/setup-hooks.sh        # Windows: scripts/setup-hooks.ps1
```

`setup-hooks` sets `core.hooksPath = .githooks`. Git cannot install hooks on
clone, so every fresh checkout must run this once.

Hook policy:

- Pre-commit is not installed by default, so checkpoint commits stay fast. Humans who want a local fast `ruff` guardrail can opt in by copying or symlinking `.githooks/optional/pre-commit` into their active hooks path.
- Pre-push is strict: it runs `scripts/preflight.sh`, which currently runs
  `uv run ruff check .`, `uv run pytest-llm`, and `uv run pyright`, then blocks
  direct `v*` tag pushes. `v*` tags are CI-owned; they are created automatically
  on PR merge to `main`.
- Humans may bypass hooks with Git's standard `--no-verify` only when they are
  doing so intentionally. LLM agents must not use `--no-verify` unless the user
  explicitly instructs them to.

## Verify

```bash
uv run meridian --version
uv run meridian doctor
```

## Install Validation

Use these when you want to verify the installed CLI behavior, not just `uv run`
from the checkout.

Snapshot install from the current checkout:

```bash
uv tool install --force . --no-cache --reinstall
```

Editable install:

```bash
uv tool install --force --editable . --no-cache --reinstall
```

Then verify the installed tool:

```bash
meridian --version
uv tool list
```

## Test

For the full local preflight used by pre-push and release preparation:

```bash
# macOS / Linux
scripts/preflight.sh

# Windows (PowerShell)
scripts\preflight.ps1
```

Fast mode (lint only):

```bash
# macOS / Linux
scripts/preflight.sh fast

# Windows (PowerShell)
scripts\preflight.ps1 fast
```

Pre-push gate (lint + type check + all tests, with optional `--quick` to skip smoke tests):

```bash
# macOS / Linux
scripts/check.sh
scripts/check.sh --quick

# Windows (PowerShell)
scripts\check.ps1
scripts\check.ps1 -quick
```

Individual checks:

```bash
uv run ruff check .
uv run pytest-llm
uv run pyright
```

## Release

Stable meridian-cli releases happen automatically when you merge a PR to `main`.

### What you do

1. Work in a worktree branch: `meridian work start "my-feature" --worktree`
2. Write changelog entries under `CHANGELOG.md` `[Unreleased]` as you commit
3. Open a PR with the PR template (`.github/PULL_REQUEST_TEMPLATE.md`)
4. Set one release label:
   - `release:patch` (default if unlabeled)
   - `release:minor`
   - `release:major`
   - `release:skip`
5. Merge the PR to `main`

### What CI does on merge

`.github/workflows/release-on-merge.yml` runs on every merged PR. It:

1. Computes the next version from existing git tags
2. Bumps `src/meridian/__init__.py`
3. Promotes `CHANGELOG.md` `[Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD`
4. Creates commit `Release X.Y.Z`
5. Creates and pushes tag `vX.Y.Z`
6. PyPI publish fires on the tag push (`publish-pypi.yml`)

### Post-merge

Clean up merged worktrees:

```bash
scripts/prune-worktrees.sh --dry-run    # preview
scripts/prune-worktrees.sh --yes        # execute
```

### Boundaries

- Do not edit `src/meridian/__init__.py` for stable releases — CI owns it
- Do not create or push `v*` tags manually — CI owns them
- The pre-push hook blocks direct `v*` tag pushes; CI bypasses this via `GITHUB_TOKEN`

## Run from source

```bash
uv run meridian --help
```

## Windows Support

Windows is a supported platform. The CLI and runtime run natively without WSL.

### CI

A `windows-gate` job in `.github/workflows/meridian-ci.yml` runs on `windows-latest` on every push, executing `uv run pytest -n auto -m "not slow"`.

### Developer scripts

PowerShell mirrors exist for the standard developer workflow:

| Task | POSIX | Windows |
|------|-------|---------|
| Setup git hooks | `scripts/setup-hooks.sh` | `scripts\setup-hooks.ps1` |
| Pre-push gate | `scripts/check.sh` | `scripts\check.ps1` |
| Full preflight | `scripts/preflight.sh` | `scripts\preflight.ps1` |

### Deferred items

The following are explicitly out of scope and not implemented:

- **ConPTY / terminal passthrough**: Primary-session TUI capture for Claude Code and Codex session-ID extraction relies on PTY semantics. Windows ConPTY support is not implemented — harness features that depend on PTY capture may not work correctly natively on Windows. WSL is the supported path for primary-session use on Windows.
- **Full bash script parity**: Only the three scripts above have PowerShell mirrors. `scripts/quality-issues.sh` and other helpers remain POSIX-only.

## Workspace conventions

Workspace config declares sibling directories that harnesses may access during launches.
Commit shared repo-layout conventions in `meridian.toml` with `[workspace.NAME]` entries. Put machine-specific overrides and additions in gitignored `meridian.local.toml`.

If your repos are not at the paths in `meridian.toml`, create `[workspace]` overrides in `meridian.local.toml`:

```toml
[workspace.frontend]
path = "/home/you/src/meridian-web"
```

Missing committed paths are silently skipped so partial checkouts work. Missing local override paths produce `workspace_local_missing_root` because they usually indicate a typo or stale local config. There is no `enabled` field and no subtractive override for disabling an existing committed entry; that limitation is intentional and can be extended later if needed.

See [docs/configuration.md](docs/configuration.md#workspace) for the full schema, projection behavior, and migration details.

## Chat Server + Frontend

The chat backend (`meridian chat`) serves the frontend from built assets by
default. For development with hot reload, use `--dev` mode.

### Quick Start

```bash
# Serve UI from built assets (end user / backend dev path)
make chat

# Dev mode with hot reload (frontend dev path)
make chat-dev
```

### Static mode (default)

`meridian chat` serves pre-built frontend assets alongside the API. No
Node.js required at runtime.

```bash
# Build frontend assets from the sibling meridian-web checkout
make build-frontend

# Serve with built assets
meridian chat --open
```

Asset resolution order:
1. `--frontend-dist <path>` explicit override
2. Packaged assets (from installed wheel)
3. `../meridian-web/dist` convenience fallback

If no assets are found, the server falls back to headless (API-only) mode.

### Dev mode

`meridian chat --dev` starts the backend and a Vite dev server with hot reload:

```bash
meridian chat --dev --open
```

Frontend root resolution:
1. `--frontend-root <path>` explicit flag
2. `MERIDIAN_DEV_FRONTEND_ROOT` env var
3. `../meridian-web` sibling convention

Set `MERIDIAN_ENV=dev` in `.env` for persistent dev mode.

### Portless integration (optional)

When [portless](https://github.com/vercel-labs/portless) is installed, dev
mode uses it automatically for stable HTTPS URLs. Portless is optional — raw
Vite on localhost is the fallback.

```bash
# Install portless (one-time)
npm install -g portless
portless trust

# Dev mode auto-detects portless
meridian chat --dev

# Force raw Vite (skip portless)
meridian chat --dev --no-portless
```

### Network sharing (dev mode only)

Share your dev UI on Tailscale or publicly via Funnel. These require
portless and are always explicit opt-in:

```bash
# Share on your tailnet
meridian chat --dev --tailscale

# Share publicly (requires Funnel ACL)
meridian chat --dev --funnel
```

If a portless route is occupied:
```bash
# Clean up stale routes
portless prune

# Or take over explicitly
meridian chat --dev --portless-force
```

### Headless mode

```bash
meridian chat --headless
# → API-only at http://127.0.0.1:<port>
```
