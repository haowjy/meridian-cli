# Development Guide: meridian-cli

No real users, no real user data. No backwards compatibility needed — completely change the schema to get it right.

## Philosophy

**Meridian-Channel** is a coordination layer for multi-agent systems

### Design Principles

1. **Separate Policy from Mechanism** *(Raymond, Rule of Separation)*: Harness adapters are mechanism (how to launch Claude/Codex/OpenCode). CLI commands are policy (what to do, which model, what output). Policy changes fast; mechanism stays stable. Keep them apart.
2. **Separate Concerns, Justify Boundaries** *(Dijkstra, Separation of Concerns)*: Group by concern — things that change together for the same reason belong together. Draw boundaries where concerns actually change independently. A boundary between things that always change together is ceremony, not structure. God-object symptoms are refactor triggers, but splitting into more types that carry the same data is not the fix — independence is.
3. **Extend Through Seams**: New harness = one adapter file + registration. New package source = one mars config entry. New CLI command = one module. New extension/plugin capability = one explicit seam, not scattered edits through core. If a feature requires editing 10 files, the abstraction is wrong.
4. **Knowledge in Data, Not Code** *(Raymond, Rule of Representation)*: Agent capabilities live in YAML profiles, not procedural code. State lives in JSONL events, not in-memory objects. This keeps the system inspectable and harness-agnostic.
5. **Crash-Only Design** *(Candea & Fox)*: Every write is atomic (tmp+rename). Every read tolerates truncation. There is no "graceful shutdown" — if meridian is killed mid-spawn, the next `meridian status` detects and reports the orphaned state. Recovery IS startup.
6. **Progressive Disclosure** *(clig.dev, Lengstorf)*: `meridian spawn "do the thing"` works with smart defaults. Power users override with `--model`, `--harness`, `--skills`. Don't force all-or-nothing configuration.
7. **Simplest Orchestration That Works** *(Google Cloud AI patterns)*: Stay a thin coordination layer. Centralized spawn-and-report is enough. Don't build complex agent choreography until the simple model breaks. "Simplest" means least total complexity owned — lines maintained, platforms tested, failure modes debugged, contributors onboarded — not fewest imports. A trusted library that deletes a subsystem is a simplification; a hand-rolled reimplementation of the same subsystem is not.
8. **Refactor Early, Change Is Cheap** *(Beck, "make the change easy")*: No external users, no real user data, and no backwards-compatibility constraints mean structural debt costs more than fixing it. LLM-driven implementation lowers the execution cost of change; accumulated context and architecture debt raise the reasoning cost. Prefer early cleanup when it reduces future edit fan-out, clarifies ownership, or makes behavior easier to observe and test.

### Core Principles

1. **Harness-Agnostic**: One CLI, many runtimes. Meridian never assumes Claude, Codex, or any specific harness — adapters bridge the gap.
2. **Files as Authority**: All state is files — project-level under `.meridian/`, user-level under `~/.meridian/` (see `get_user_home()`). No databases, no services, no hidden state. If it's not on disk, it doesn't exist.
3. **Coordination, Not Control**: Meridian provides structure (spawns, sessions, skills, sync) but never dictates how agents do their work.
4. **Observable by Default, Intrusive Only Where Observation Requires It**: Meridian reads harness state rather than driving harness behavior. Where observation needs a mechanism the harness doesn't provide — like capturing output from a TUI that only emits to a TTY — meridian reaches into the boundary with the minimum machinery needed (PTY capture for primary-launch session-ID extraction is the canonical example). Observability requirement, not control lever. Code that looks intrusive should be justified against a specific unobservable-otherwise constraint, and that constraint named in the commit or comment.
5. **Idempotent Operations**: `meridian sync` twice = same result. Re-running after a crash converges to correct state, never doubles side effects.
6. **Windows Is First-Class**: Windows support is a product requirement, not cleanup work. Do not ship path logic, process behavior, filesystem assumptions, or tests that only work on POSIX unless the limitation is explicitly accepted and documented. Design root discovery, env handling, locking, signals, shell invocation, and smoke-test coverage with Windows semantics in mind from the start.
7. **Prefer Cross-Platform Abstractions**: In Rust, default to `std` and mature cross-platform crates over handwritten OS-specific branches. Use direct platform-specific APIs only behind narrow adapter boundaries and only when a cross-platform abstraction is insufficient. A dependency that deletes platform-specific code and test matrix burden is a simplification, not bloat.
8. **No VCS Dependency for Core Functionality**: Meridian must work in plain directories without git. Git metadata (remotes, commit history, worktree structure) may be used as optional hints or heuristics, but core operations — project identity, state resolution, spawn coordination, session tracking — must not require a git repository.

### Architecture

- **State Root**: User-level `~/.meridian/` (via `get_user_home()`) is the primary state root — spawns, sessions, work items. Project-level `.meridian/` holds project identity and package config. Migration toward user-level is intentional and ongoing.
- **Harness Adapters**: `src/meridian/lib/harness/` — per-harness command building, output extraction, materialization. Adding a harness = one adapter file + registration.
- **State Layer**: `src/meridian/lib/state/` — path resolution, spawn store, session store. Atomic writes via tmp+rename, `fcntl.flock` for concurrency.
- **Package Sync**: `meridian mars ...` — package resolution and configured target materialization are delegated to mars.
- **Profiles & Skills**: Agent profiles (YAML markdown) define capabilities, model, and skills. Skills load fresh on launch/resume (survives compaction).

## Dev Workflow

NEVER REVERT CHANGES — always assume it's someone else's work.

## Git Hooks

Run `scripts/setup-hooks.sh` (or `scripts/setup-hooks.ps1` on Windows) once after cloning.
This sets `core.hooksPath = .githooks`; Git cannot auto-install hooks on clone.
See `DEVELOPMENT.md` for the human-facing setup and release workflow.

Hook policy:
- Pre-commit is not installed by default; optional fast ruff helper lives at `.githooks/optional/pre-commit` for humans who opt in locally.
- Pre-push is strict: full `scripts/preflight.sh` plus direct `v*` tag push guard.
- Stable `v*` tags are CI-owned (`.github/workflows/release-on-merge.yml`).

**NEVER use `--no-verify` on git push unless explicitly instructed by the user.**

**NEVER manually create or push git tags matching `v*`.** Tags come from CI after normal pushes to main.

### Pull Requests

When creating or updating a GitHub PR, read `.github/PULL_REQUEST_TEMPLATE.md` and fill every section. Do not use any other PR body format — the built-in generic format is wrong for this project. Sections: why, goal, summary, resulting behavior, changes, work item, verification, knowledge updates, spawn trace, release label, cleanup. Keep the PR body current as the branch evolves.

Always set a `release:*` label on the PR before merging:

| Label | Effect |
|---|---|
| `release:patch` / `release:stable` | Stable patch bump (default for most work) |
| `release:minor` | Stable minor bump |
| `release:major` | Stable major bump |
| `release:rc` | Prerelease (RC) |
| `release:skip` | No release for this merge |

A merged PR with no `release:*` label defaults to a prerelease (RC); unknown labels also default to RC. Use `release:skip` to ship without any release. (A commit with no associated PR — e.g. a direct push — still skips auto-release.)

### Release workflow

**Read `docs/releasing.md` before releasing, pushing to main, or opening a PR.**
It covers the PR template, release labels, backfill/direct-to-main releases, and
the `push.followTags` gotcha that causes the pre-push hook to block manual releases.

```bash
# Normal path: open a PR from a worktree branch and merge to main.
# CI bumps the patch version, promotes changelog, commits Release X.Y.Z, and tags vX.Y.Z.
# Agents write notes under CHANGELOG.md [Unreleased], but must not edit __version__ or create tags.

# Backfill (direct-to-main, no PR): use the manual release helper.
scripts/manually-release.sh patch --push

# Post-merge cleanup:
scripts/prune-worktrees.sh
```

### Logging Convention

Catalog/config modules (`meridian.lib.catalog.*`, `meridian.lib.config.*`) use stdlib `logging.getLogger(__name__)`. Ops/launch/harness modules (`meridian.lib.ops.*`, `meridian.lib.launch.*`, `meridian.lib.harness.*`) use `structlog.get_logger()`. Split matters: launch diagnostic boundary (`capture_library_diagnostics()` in `diagnostics.py`) captures stdlib warnings during spawn/launch; structlog bypasses it, so catalog/config structlog warnings leak to stderr.

### Editing Agents & Skills

**NEVER edit generated target directories directly** (for this repo: `.claude/agents/`, `.claude/skills/`, `.cursor/`, `.codex/`, `.opencode/`, `.pi/`) — they are generated output, overwritten by `meridian mars sync`. Edit the source package repos directly in sibling checkouts. Preferred local layout:

- `../meridian-cli` (this repo)
- `../meridian-web` — general-purpose agent frontend (Apache-2.0). Makefile `frontend` target expects this at `$MERIDIAN_WEB` (default `../meridian-web`).
- `../prompts/meridian-base`
- `../prompts/meridian-dev-workflow`
- `../mars-agents`

### Config Precedence

CLI flags > ENV vars > YAML profile > Project config > User config > harness default.

This applies to **every resolved field independently** — model, harness, approval, timeout, skills. Derived fields inherit the precedence level of their source: if the user overrides the model with `-m`, the harness must be derived from the overridden model, not from the profile's harness. A profile-level value must never win over a CLI override, even indirectly.

### Testing

Prefer smoke tests over unit tests. Unit tests are for logic that's hard to smoke test — signals, concurrency, security/env sanitization, sync engine algorithms, parsing edge cases. See `tests/AGENTS.md` for test placement and available fakes.

Cross-platform tests must use project abstractions (`get_user_home()`, `get_home_path()`, `Path`, `os.pathsep`) — never hardcode POSIX paths or assume Unix-only semantics.

```bash
uv sync --extra dev      # Install from source
uv run ruff check .      # Lint
uv run pytest-llm        # Unit tests (token-efficient output)
uv run --extra dev python -m pyright            # Type check (must be 0 errors)
uv run meridian           # Smoke test the CLI directly
uv add <package>          # Add a dependency (never use pip install)
```

### Releasing Prompt Packages

Prompt packages (meridian-base, meridian-dev-workflow, meridian-prompter) use `meridian mars version` to release. It bumps `mars.toml`, promotes CHANGELOG.md `[Unreleased]` → `[X.Y.Z] - YYYY-MM-DD`, commits, and tags in one step.

```bash
# From the prompt repo root:
meridian mars version patch              # bump, commit, tag locally
meridian mars version patch --push       # bump, commit, tag, push to origin
meridian mars version minor --push       # minor bump when scope warrants it
```

Update CHANGELOG.md entries under `[Unreleased]` as you work — `meridian mars version` promotes them automatically. If `[Unreleased]` is empty, it warns but proceeds.

### Releasing mars-agents and meridian-cli

- `meridian-cli`: stable releases are main-push auto-release (`release-on-merge.yml`).
- `mars-agents`: stable releases are main-push auto-release (`release-on-main.yml`).

**Direct push ≠ release.** A plain `git push` to `main` with no associated merged PR runs CI/CD only — both workflows resolve release intent from the trigger commit's PR (`/repos/.../commits/{sha}/pulls`); no PR found → `should_release=false`, so no version bump or tag. Auto-release fires **only** for a merged PR carrying a `release:*` label. Direct-to-main backfill must use the manual release helper (`scripts/manually-release.sh`).

Do not use `meridian mars version` for these repos — that's for prompt packages only.

## Always Use `uv`

This project uses `uv` exclusively for Python tooling. Never use `pip`, `pip install`, `python`, or `python -m` directly. Use `uv run`, `uv add`, and `uv sync` instead.

## Quality Issue Triage

To see current quality/immediate burn-down work on GitHub, use:

```bash
scripts/quality-issues.sh
```

Lists open issues on `haowjy/meridian-cli`, excludes issues labelled `future`, and groups by `quality:high`, `quality:medium`, `quality:low`, then unprioritized. Mars capability packaging issues appear here by default — they are not future work unless explicitly labelled `future`.

### Cross-Platform Paths

Meridian has centralized cross-platform path handling. **Do not write new platform-detection code** — use the existing primitives:

- **`src/meridian/lib/platform/__init__.py`**: `IS_WINDOWS`, `get_home_path()`
- **`src/meridian/lib/state/user_paths.py`**: `get_user_home()`

User state root resolution:
1. `MERIDIAN_HOME` env var (if set)
2. Windows: `%LOCALAPPDATA%\meridian`
3. Windows fallback: `%USERPROFILE%\AppData\Local\meridian`
4. POSIX fallback: `~/.meridian`

When adding features that need user-level storage (git clones, cache, etc.), put them under `get_user_home()`:

```python
from meridian.lib.state.user_paths import get_user_home

repos_dir = get_user_home() / "git"  # Cross-platform correct
```

Do not hardcode `~/.meridian/` or introduce new `LOCALAPPDATA` / `XDG_DATA_HOME` branches.
