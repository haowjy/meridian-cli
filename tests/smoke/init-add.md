# Smoke: init --add

`meridian init --add` orchestration: config bootstrap, package install, auto-link, and primary agent setup in one command.

## Prerequisites

`mars` binary must be in PATH. If not installed, the mars-dependent tests will error on the mars executable check.

## Setup

```bash
. tests/smoke/scripts/setup.sh --git
```

Create a local test package to avoid network dependency:

```bash
PKG=$(mktemp -d)
cat > "$PKG/mars.toml" << 'EOF'
[package]
name = "smoke-test-pkg"
version = "0.1.0"
primary_agent = "test-agent"

[package.targets]
required = [".claude"]
EOF

mkdir -p "$PKG/agents"
printf '# test-agent\n' > "$PKG/agents/test-agent.md"
```

## Full init --add flow

```bash
uv run meridian init --add "$PKG" "$SCRATCH"
```
- [ ] Exit 0
- [ ] `$SCRATCH/meridian.toml` exists
- [ ] `$SCRATCH/mars.toml` exists
- [ ] `$SCRATCH/.claude/` directory exists (auto-linked from package target)
- [ ] `$SCRATCH/meridian.toml` contains `agent = "test-agent"` (primary agent set)
- [ ] stdout contains `Initialized`
- [ ] stdout contains `agents` (content summary line)

## Idempotent re-run

```bash
uv run meridian init --add "$PKG" "$SCRATCH"
```
- [ ] Exit 0
- [ ] stderr is empty
- [ ] `$SCRATCH/meridian.toml` still exists
- [ ] `$SCRATCH/.claude/` still exists

## Explicit --link overrides auto-link

```bash
SCRATCH2=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH2
uv run meridian init --add "$PKG" --link .codex "$SCRATCH2"
```
- [ ] Exit 0
- [ ] `$SCRATCH2/.codex/` exists
- [ ] `$SCRATCH2/.claude/` does NOT exist (explicit --link overrides package targets)

## Bare init regression (no --add)

```bash
SCRATCH3=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH3
uv run meridian init "$SCRATCH3"
```
- [ ] Exit 0
- [ ] `$SCRATCH3/meridian.toml` exists
- [ ] `$SCRATCH3/mars.toml` does NOT exist (bare init does not touch mars)

## JSON output

```bash
SCRATCH4=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH4
uv run meridian --json init --add "$PKG" "$SCRATCH4"
```
- [ ] Exit 0
- [ ] stdout is valid JSON
- [ ] JSON contains `"ok": true`
- [ ] JSON contains `"targets_linked"` array
- [ ] JSON contains `"content"` object with content type keys (e.g. `"agents"`)
- [ ] JSON contains `"primary_agent"` object
