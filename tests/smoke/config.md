# Smoke: config

Config init, show, get, set, and reset.

## Setup (no git)

```bash
. tests/smoke/scripts/setup.sh
```

## Setup (git required for init/set/reset)

```bash
. tests/smoke/scripts/setup.sh --git
```

## config init

```bash
uv run meridian config init
```
- [ ] Exit 0
- [ ] stdout: `created: <path>/meridian.toml`
- [ ] `$SCRATCH/meridian.toml` exists on disk

## config show

```bash
uv run meridian config show --json
```
- [ ] Exit 0
- [ ] `project_root` equals `$SCRATCH` (forward slashes)
- [ ] `project_root_source == "env"`
- [ ] `runtime_root` is `null`
- [ ] `values` array contains entry for `defaults.harness` with `value == "codex"` and `source == "builtin"`

## config get — single key

```bash
uv run meridian config get defaults.harness
```
- [ ] Exit 0
- [ ] stdout: `defaults.harness: codex [source: builtin]`

## config set and get roundtrip

```bash
uv run meridian config init
uv run meridian config set defaults.model smoke-test-model
```
- [ ] Exit 0
- [ ] stdout contains `set defaults.model = smoke-test-model`

```bash
uv run meridian config get defaults.model
```
- [ ] Exit 0
- [ ] stdout: `defaults.model: smoke-test-model [source: file]`
- [ ] `$SCRATCH/meridian.toml` contains `model = "smoke-test-model"`

## config reset

```bash
uv run meridian config init
uv run meridian config set defaults.model to-be-reset
uv run meridian config reset defaults.model
```
- [ ] Exit 0
- [ ] stdout contains `reset defaults.model (removed)`

```bash
uv run meridian config get defaults.model
```
- [ ] Exit 0
- [ ] stdout: `defaults.model:  [source: builtin]`

## config set preserves unrelated sections

```bash
cat > "$SCRATCH/meridian.toml" << 'EOF'
[defaults]
harness = "claude"

[workspace.docs]
path = "./docs"

[[hooks]]
event = "spawn"
run = "echo hi"
EOF

uv run meridian config set defaults.harness opencode
```
- [ ] Exit 0
- [ ] `meridian.toml` contains `harness = "opencode"`
- [ ] `meridian.toml` still contains `[workspace.docs]`
- [ ] `meridian.toml` still contains `[[hooks]]`
