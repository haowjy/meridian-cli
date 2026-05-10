# Smoke: workspace

Workspace init and inspection — idempotency, config surface, doctor warnings.

## Setup (git required)

```bash
. tests/smoke/scripts/setup.sh --git
```

## workspace init — creates local scaffold and updates gitignore

```bash
echo 'model = "gpt-5"' > "$SCRATCH/meridian.local.toml"
uv run meridian workspace init
```
- [ ] Exit 0
- [ ] stdout contains `created:`
- [ ] stdout contains `local_gitignore:`
- [ ] stdout contains `(updated)`
- [ ] `meridian.local.toml` still contains `model = "gpt-5"` (not clobbered)
- [ ] `meridian.local.toml` contains exactly one `[workspace.example]` section
- [ ] `.git/info/exclude` contains `meridian.local.toml` exactly once

## workspace init — idempotent on re-run

```bash
uv run meridian workspace init
```
- [ ] Exit 0
- [ ] stdout contains `exists:`
- [ ] stdout contains `(ok)`
- [ ] `meridian.local.toml` still has exactly one `[workspace.example]` section

## config show surfaces workspace status after init

```bash
uv run meridian workspace init
uv run meridian config show --json
```
- [ ] Exit 0
- [ ] `workspace.status == "none"`
- [ ] `workspace.sources` is an empty array
- [ ] `workspace.roots` is `{"count": 0, "projected": 0, "skipped": 0}`
- [ ] `workspace_findings` is an empty array

## doctor reports missing local workspace root

```bash
mkdir -p "$SCRATCH/.mars/agents" "$SCRATCH/.mars/skills"
cat > "$SCRATCH/meridian.local.toml" << 'EOF'
[workspace.missing]
path = "./missing-local"
EOF

uv run meridian doctor --json
```
- [ ] Exit 0
- [ ] `ok` is `false`
- [ ] `warnings` contains an entry with `code == "workspace_local_missing_root"`
- [ ] That entry's `payload.name == "missing"`
- [ ] That entry's `payload.path` ends with `/missing-local`
- [ ] That entry's `message` contains `does not exist`

## Invalid workspace name blocks spawn dry-run with guidance

```bash
mkdir -p "$SCRATCH/.mars/agents"
echo "# Test" > "$SCRATCH/.mars/agents/test.md"
cat > "$SCRATCH/meridian.toml" << 'EOF'
[workspace.Bad]
path = "./root"
EOF

uv run meridian spawn -a test -p "test" --dry-run --json
```
- [ ] Exit non-zero
- [ ] stderr JSON `error` field contains `Invalid workspace config in meridian.toml.`
- [ ] stderr JSON `error` field contains `entry name 'Bad'`
- [ ] stderr JSON `error` field contains `meridian config show`
- [ ] stderr JSON `error` field contains `meridian doctor`
