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

- Pre-commit is intentionally lightweight: fast `ruff` only. It is a guardrail,
  not the full verification gate, so checkpoint commits stay fast.
- Pre-push is strict: it runs `scripts/preflight.sh`, which currently runs
  `uv run ruff check .`, `uv run pytest-llm`, and `uv run pyright`, then blocks
  direct `v*` tag pushes. Use `scripts/release.sh` for release tags.
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
scripts/preflight.sh
```

Individual checks:

```bash
uv run ruff check .
uv run pytest-llm
uv run pyright
```

## Release

Use the release helper to run preflight, bump the package version, create the
release commit, wait for CI on stable releases, and create the matching
`v<version>` tag. Do not create or push `v*` tags manually.

The package version currently lives in `src/meridian/__init__.py` as
`__version__`.

```bash
# Stable release: prepare, push, wait for CI, then tag
scripts/release.sh prepare patch --push

# If CI fails after prepare, fix forward, then resume without rerunning local preflight
scripts/release.sh resume --push

# RC release: no CI gate
scripts/release.sh prepare rc --push

# Inspect or abandon prepared release state
scripts/release.sh status
scripts/release.sh abort
```

## Run from source

```bash
uv run meridian --help
```

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
