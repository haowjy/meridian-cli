# Smoke: plain directory roots

Root authority discovery in plain directories (no git required).

## Setup

```bash
# These tests use custom directory layouts — no shared SCRATCH
```

## Nested cwd resolves to project root via meridian.toml walk

```bash
PROJECT=$(mktemp -d)
NESTED="$PROJECT/src/feature"
mkdir -p "$NESTED"
cat > "$PROJECT/meridian.toml" << 'EOF'
[defaults]
harness = "codex"
EOF
HOME_DIR=$(mktemp -d)

# Run config show from nested cwd — should resolve root to $PROJECT
cd "$NESTED"
MERIDIAN_HOME="$HOME_DIR" uv run meridian --json config show
```
- [ ] Exit 0
- [ ] `project_root` equals `$PROJECT` (not `$NESTED`)

```bash
# workspace init from same nested cwd should create $PROJECT/meridian.local.toml
MERIDIAN_HOME="$HOME_DIR" uv run meridian workspace init
```
- [ ] Exit 0
- [ ] `$PROJECT/meridian.local.toml` exists (created at project root, not nested dir)

## Directory under $HOME/.meridian does not misroot to user state dir

```bash
HOME_ROOT=$(mktemp -d)
NESTED="$HOME_ROOT/notes/daily"
mkdir -p "$HOME_ROOT/.meridian" "$NESTED"

# Run from $NESTED without MERIDIAN_HOME; HOME set to $HOME_ROOT
cd "$NESTED"
HOME="$HOME_ROOT" uv run meridian --json config show
```
- [ ] Exit 0
- [ ] `project_root` is NOT `$HOME_ROOT` (user state dir must not be used as project root)
- [ ] `project_root_source` is NOT `"project-state"`
