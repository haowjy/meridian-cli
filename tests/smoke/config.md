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
- [ ] `values` array contains entry for `primary.agent` with `value == null` and `source == "builtin"`

## config get — single key

```bash
uv run meridian config get primary.agent
```
- [ ] Exit 0
- [ ] stdout: `primary.agent: null [source: builtin]`

## config set and get roundtrip

```bash
uv run meridian config init
uv run meridian config set primary.agent reviewer
```
- [ ] Exit 0
- [ ] stdout contains `set primary.agent = reviewer`

```bash
uv run meridian config get primary.agent
```
- [ ] Exit 0
- [ ] stdout: `primary.agent: reviewer [source: file]`
- [ ] `$SCRATCH/meridian.toml` contains `agent = "reviewer"`

## config reset

```bash
uv run meridian config init
uv run meridian config set primary.agent to-be-reset
uv run meridian config reset primary.agent
```
- [ ] Exit 0
- [ ] stdout contains `reset primary.agent (removed)`

```bash
uv run meridian config get primary.agent
```
- [ ] Exit 0
- [ ] stdout: `primary.agent: null [source: builtin]`

## config set preserves unrelated sections

```bash
cat > "$SCRATCH/meridian.toml" << 'EOF'
[primary]
agent = "reviewer"

[workspace.docs]
path = "./docs"

[[hooks]]
event = "spawn"
run = "echo hi"
EOF

uv run meridian config set primary.agent coder
```
- [ ] Exit 0
- [ ] `meridian.toml` contains `agent = "coder"`
- [ ] `meridian.toml` still contains `[workspace.docs]`
- [ ] `meridian.toml` still contains `[[hooks]]`
