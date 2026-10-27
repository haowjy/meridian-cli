# Development

## Setup

```bash
git clone https://github.com/haowjy/meridian-cli.git
cd meridian-cli
uv sync --extra dev
scripts/setup-hooks.sh
```

`setup-hooks` sets `core.hooksPath = .githooks`. Git cannot install hooks on
clone, so every fresh checkout must run this once.

Hook policy:

- Pre-commit is not installed by default, so checkpoint commits stay fast. Humans who want a local fast `ruff` guardrail can opt in by copying or symlinking `.githooks/optional/pre-commit` into their active hooks path.
- Pre-push blocks direct `v*` tag pushes (`v*` tags are CI-owned; they are
  created automatically after normal pushes to `main`) and runs the prepared
  60-second routine gate: Ruff, Pyright, and the risk-based default pytest
  paths. Run `scripts/preflight.sh extended` explicitly before a release or
  when you need the complete suite, Pi extension build, and package build.
- Git hooks run without a terminal, so every preflight step must run without
  prompting. Scope any automation setting to the step that needs it; don't export
  it for the whole gate.
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

For the routine local preflight used by pre-push:

```bash
scripts/preflight.sh
```

The fast gate's 60-second budget is a target for Ruff, Pyright, pytest,
pytest-xdist startup, and command overhead on a prepared machine; the gate warns
when it runs over and never fails or stops checks because of time.

For the complete opt-in preflight used before release promotion or on demand:

```bash
scripts/preflight.sh extended
# `full` remains a compatibility alias for existing release helpers.
scripts/preflight.sh full
```

The default pytest paths are risk-based directory boundaries. To run every
automated test explicitly (including `tests/extended/`), use:

```bash
uv run pytest tests/
uv run pytest-llm tests/
```

`scripts/check.sh` delegates to the same fast preflight as the hook; it does
not maintain a second test pipeline. `tests/smoke/` and `tests/e2e/` remain
manual/runtime workflows, documented in their local guides.

Individual checks:

```bash
uv run ruff check .
uv run pytest-llm
uv run --extra dev python -m pyright --warnings
```

## Release

Releases are automatic from PR merges to `main`. Write the changelog entry in the
PR body's `## Changelog` section, set one `release:*` label, and merge. Do not edit
`CHANGELOG.md`, `__version__`, or `v*` tags by hand: CI owns them. The labels, the
integration-branch flow for breaking changes, the PR-body gate, and the manual
backfill helper are in [docs/releasing.md](docs/releasing.md).

### Post-merge

Clean up merged worktrees:

```bash
scripts/prune-worktrees.sh --dry-run    # preview
scripts/prune-worktrees.sh --yes        # execute
```

## Run from source

```bash
uv run meridian --help
```

## Platform Support

Linux and macOS are supported. WSL is supported as a Linux environment. Native
Windows was never made to work and is not planned; existing Windows branches and
scripts are untested, best-effort legacy code.

## Workspace conventions

Workspace config declares sibling directories that harnesses may access during launches.
Commit shared repo-layout conventions in `meridian.toml` with `[workspace.NAME]` entries. Put machine-specific overrides and additions in gitignored `meridian.local.toml`.

If your repos are not at the paths in `meridian.toml`, create `[workspace]` overrides in `meridian.local.toml`:

```toml
[workspace.prompts]
path = "/home/you/src/prompts/meridian-base"
```

Missing committed paths are silently skipped so partial checkouts work. Missing local override paths produce `workspace_local_missing_root` because they usually indicate a typo or stale local config. There is no `enabled` field and no subtractive override for disabling an existing committed entry; that limitation is intentional and can be extended later if needed.

See [docs/configuration.md](docs/configuration/workspace.md) for the full schema, projection behavior, and migration details.

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
