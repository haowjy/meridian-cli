# Development

## Setup

```bash
git clone https://github.com/haowjy/meridian-cli.git
cd meridian-cli
uv sync --extra dev
scripts/setup-hooks.sh        # Windows: scripts/setup-hooks.ps1
```

`setup-hooks` sets `core.hooksPath = .githooks`. Git cannot install hooks on
clone, so every fresh checkout must run this once.

Hook policy:

- Pre-commit is not installed by default, so checkpoint commits stay fast. Humans who want a local fast `ruff` guardrail can opt in by copying or symlinking `.githooks/optional/pre-commit` into their active hooks path.
- Pre-push is strict: it runs `scripts/preflight.sh`, which currently runs
  `uv run ruff check .`, `uv run --extra dev python -m pyright`, `uv run pytest -x -q`, and
  `uv build --no-sources`, then blocks direct `v*` tag pushes. `v*` tags are
  CI-owned; they are created automatically after normal pushes to `main`.
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

> `uv tool install` does **not** build Pi extensions. If your spawns use the
> `pi` harness, build them first (see [Pi Extensions](#pi-extensions)) — the
> wheel only bakes in whatever `dist/extensions/*` already exists on disk, so a
> missing build ships no extensions and a stale build ships old behavior.

Editable install:

```bash
uv tool install --force --editable . --no-cache --reinstall
```

Then verify the installed tool:

```bash
meridian --version
uv tool list
```

## Pi Extensions

The `pi` harness loads two bundled extensions — `managed-bash` and
`meridian-spawn-watch` — from TypeScript sources under
`src/meridian/pi_runtime/extensions/`. They are bundled to ESM with `tsup` into
`src/meridian/pi_runtime/dist/extensions/<name>/index.js`.

`dist/` is gitignored, and nothing in the Python toolchain builds it — neither
`uv sync`, `uv build`, nor `uv tool install` runs the bundler. Build it
explicitly:

```bash
corepack enable                       # provides pnpm
cd src/meridian/pi_runtime
pnpm install --frozen-lockfile        # first time, or after a lockfile change
pnpm run build:extensions             # bundle TS -> dist/extensions/*.js
```

Rebuild after editing any `.ts` under `extensions/`. To build and run the
extension tests in one step:

```bash
pnpm run verify:extensions            # build + vitest
```

When this matters:

- **Local dev with `uv run meridian`** — the launcher resolves extensions from
  the source-tree `dist/` first, so a rebuild takes effect immediately with no
  reinstall.
- **`uv tool install` from the checkout** — build first, then install. A missing
  build ships no extensions and `pi` spawns fail at launch with
  `PiExtensionProjectionError`; a stale build silently ships old behavior. The
  wheel picks up `dist/extensions/*` via `[tool.hatch.build] artifacts` in
  `pyproject.toml`.
- **PyPI releases** — no action needed. `release-on-merge.yml` and every
  `meridian-ci.yml` job run `pnpm run build:extensions` from source before
  building the wheel, so published wheels always carry freshly-built extensions.
  Do not commit `dist/`.

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
uv run --extra dev python -m pyright
```

## Release

meridian-cli releases happen automatically after normal PR merges to `main` when the PR has a `release:*` label.

### What you do

1. Work in a worktree branch: `meridian work start "my-feature" --worktree`
2. Write changelog entries under `CHANGELOG.md` `[Unreleased]` as you commit
3. Open a PR with the PR template (`.github/PULL_REQUEST_TEMPLATE.md`)
4. Set one release label:
   - `release:rc` for the default safe prerelease path
   - `release:patch` or `release:stable` for stable patch release
   - `release:skip` for no release
5. Merge the PR to `main`

PR merges without a `release:*` label skip auto-release. Direct pushes to `main`
also skip auto-release because there is no PR label to inspect.

Put `release:skip` in the pushed head commit message to skip auto-release even
when a release label is present.

### What CI does on main push

`.github/workflows/release-on-merge.yml` runs on every normal push to `main`. It:

1. Reads the selected merged PR label for the pushed commit
2. Defaults unknown `release:*` labels to RC
3. Skips when no `release:*` label is present or when `release:skip` is present
4. Computes the next stable patch or next RC from existing git tags
5. Bumps `src/meridian/__init__.py`
6. Promotes `CHANGELOG.md` `[Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD` or `## [X.Y.Z-rc.N] - YYYY-MM-DD`
7. Creates commit `release: vX.Y.Z` or `release: vX.Y.Z-rc.N`
8. Creates and pushes tag `vX.Y.Z` or `vX.Y.Z-rc.N`
9. PyPI publish runs through the tag-triggered `.github/workflows/release.yml`

The workflow skips its own `release: v...` commit so release auto-commits do not
trigger another release. `release.yml` is the only PyPI trusted publishing workflow identity.

### Post-merge

Clean up merged worktrees:

```bash
scripts/prune-worktrees.sh --dry-run    # preview
scripts/prune-worktrees.sh --yes        # execute
```

### Boundaries

- Do not edit `src/meridian/__init__.py` for stable releases — CI owns it
- Do not create or push `v*` tags manually — CI owns them
- The pre-push hook blocks direct `v*` tag pushes; GitHub Actions creates release tags without running local hooks

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
| Prune worktrees | `scripts/prune-worktrees.sh` | `scripts\prune-worktrees.ps1` |

### Deferred items

The following are explicitly out of scope and not implemented:

- **ConPTY / terminal passthrough**: Primary-session TUI capture for Claude Code and Codex session-ID extraction relies on PTY semantics. Windows ConPTY support is not implemented — harness features that depend on PTY capture may not work correctly natively on Windows. WSL is the supported path for primary-session use on Windows.
- **Full bash script parity**: Only the scripts listed above have PowerShell mirrors. `scripts/quality-issues.sh` and other helpers remain POSIX-only.

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

## Workstation: memory-safe ripgrep

`rg` on this machine is wrapped at `~/.local/bin/rg` to prevent OOM kills. A bare `rg` over a large codebase can mmap hundreds of GB of virtual address space and exhaust RAM (this happened — it killed a tmux session).

The wrapper enforces:
- `--no-mmap` — sequential reads instead of memory-mapped I/O, keeps VSZ low
- `--max-filesize 500M` — skips files larger than 500 MB
- `-j 4` — caps parallel search threads
- cgroup limits via `systemd-run`: `MemoryHigh=6G`, `MemoryMax=10G`, `MemorySwapMax=4G`

To bypass (e.g. searching a legitimately large file):
```bash
~/.local/bin/rg.real [flags] [pattern] [path]
```

Also recommended — install `earlyoom` as a system-wide backstop:
```bash
sudo apt install earlyoom
# In /etc/default/earlyoom:
# EARLYOOM_ARGS="-m 4,3 -s 10,5 --prefer '(^|/)(rg|ripgrep)$' --sort-by-rss -g -r 60"
sudo systemctl enable --now earlyoom
```
