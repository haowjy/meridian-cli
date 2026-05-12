# Smoke test helper scripts

Shell helpers for smoke testing meridian. Source them at the top of a
terminal session before working through any `tests/smoke/*.md` guide.

## Quick start

```bash
# Plain isolated environment
. tests/smoke/scripts/setup.sh

# With a git repo in SCRATCH
. tests/smoke/scripts/setup.sh --git

# Add assertion helpers too
. tests/smoke/scripts/assert.sh
```

---

## setup.sh

Sets three environment variables, then stays out of the way:

| Variable | Value |
|---|---|
| `SCRATCH` | fresh `mktemp -d` directory |
| `MERIDIAN_HOME` | fresh `mktemp -d` directory |
| `MERIDIAN_PROJECT_DIR` | same as `SCRATCH` |

**Options**

- `--git` — runs `git init --quiet` in `SCRATCH` before returning. Required
  by guides that test git-aware features (workspace, hooks, config set/reset).

**Helper function**

```bash
smoke_add_agent NAME
```

Creates `$SCRATCH/.mars/agents/NAME.md` containing `# NAME`. Use wherever
a guide's setup block creates a minimal agent profile.

```bash
. tests/smoke/scripts/setup.sh
smoke_add_agent reviewer
smoke_add_agent test
```

---

## assert.sh

Assertion helpers for scripted verification. Each function prints `PASS` or
`FAIL` and accumulates counts. Call `smoke_summary` at the end to print totals
and exit non-zero if any check failed.

### assert_exit EXPECTED ACTUAL [label]

```bash
output=$(uv run meridian config show --json); rc=$?
assert_exit 0 $rc "config show exits 0"
```

### assert_contains HAYSTACK NEEDLE [label]

```bash
assert_contains "$output" '"status": "dry-run"' "dry-run status present"
```

### assert_not_contains HAYSTACK NEEDLE [label]

```bash
assert_not_contains "$output" "Traceback" "no traceback"
```

### assert_json FIELD EXPECTED JSON [label]

Dot-separated key path into a JSON string. Uses `python3` — no `jq` needed.

```bash
assert_json "status" "dry-run" "$output"
assert_json "model_selection.requested_token" "gpt-5.5" "$output"
```

### assert_file_exists PATH [label]

```bash
assert_file_exists "$SCRATCH/meridian.toml" "config file created"
```

### smoke_summary

Print totals and return 1 if any check failed:

```bash
smoke_summary
```

---

## Example session

```bash
. tests/smoke/scripts/setup.sh
. tests/smoke/scripts/assert.sh
smoke_add_agent reviewer

out=$(uv run meridian spawn -a reviewer -p "hello" --dry-run --json); rc=$?
assert_exit 0 $rc
assert_contains "$out" '"status": "dry-run"'
assert_json "status" "dry-run" "$out"

smoke_summary
```
