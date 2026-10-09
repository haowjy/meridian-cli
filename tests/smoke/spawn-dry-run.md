# Smoke: spawn dry-run

Check CLI-visible prompt assembly without a model request. Dry-run can probe
installed harnesses or refresh catalogs; it is not part of the cheap offline
smoke script. Set `DRY_RUN_MODEL` to an available Codex model before starting.

Routing/precedence matrices remain in [compiler](../unit/launch/test_compiler.py)
and [task-dir](../integration/ops/test_task_dir_commands.py) regression tests.
For actual Mars routing provenance, use the [packaged-workspace guide](../e2e/spawn/routing-provenance.md).

## Setup

Run from the checkout in a fresh shell; all fixture paths stay under `SMOKE_ROOT`.
If native eligibility requires login, set `CODEX_AUTH_FILE` to your auth.json
before setup to deliberately copy only that file into the disposable store.

```bash
: "${DRY_RUN_MODEL:?Set DRY_RUN_MODEL to an available Codex model}"
export DRY_RUN_MODEL
. tests/smoke/scripts/setup.sh
trap smoke_cleanup EXIT
smoke_add_agent reviewer
printf '[settings]\ntargets = [".codex"]\n' > "$SCRATCH/mars.toml"
if [[ -n "${CODEX_AUTH_FILE:-}" ]]; then
  install -m 600 "$CODEX_AUTH_FILE" "$CODEX_HOME/auth.json"
fi
```

## Basic prompt and goal preview

```bash
uv run meridian spawn -a reviewer --harness codex -m "$DRY_RUN_MODEL" \
  -p "Write hello world" --goal "ship phase 3" --dry-run --json
```

- [ ] Exit 0; `status == "dry-run"`; `composed_prompt` contains `Write hello world`
- [ ] `model` present; `terminal_surface_mode == "pty_mediated"`
- [ ] `goal == "ship phase 3"`; `goal_contract_preview` includes `# Spawn Goal` and the goal

## Pi RPC/native projection and runtime guidance

Set `PI_DRY_RUN_MODEL` to a model exposed by your Pi installation.

```bash
: "${PI_DRY_RUN_MODEL:?Set an available Pi model}"
uv run meridian spawn --harness pi -m "$PI_DRY_RUN_MODEL" \
  -p "Pi projection check" --dry-run --json
uv run meridian --harness pi -m "$PI_DRY_RUN_MODEL" --dry-run --json
```

- [ ] Spawned `cli_command` begins with `pi --mode rpc`; native primary argv begins
      with `pi` and omits `--mode rpc`
- [ ] RPC argv contains `-e` for `meridian-spawn-watch`; native argv contains `-e` for both
      `managed-bash` and `meridian-spawn-watch`
- [ ] Incompatible Pi fails before launch with `pi update` / `MERIDIAN_PI_BINARY` guidance

## Template substitution and reference file

```bash
printf '# Reference\n' > "$SMOKE_ROOT/reference.md"
uv run meridian spawn -a reviewer --harness codex -m "$DRY_RUN_MODEL" \
  -p "Review {{FILE_PATH}} for {{CONCERN}}" \
  --prompt-var FILE_PATH=src/main.py --prompt-var CONCERN=security \
  -f "$SMOKE_ROOT/reference.md" --dry-run --json
```

- [ ] Exit 0; prompt contains `src/main.py` and `security`, not either template token
- [ ] Reference filename appears in JSON or its `reference_files` array

## Task CWD, relative references and authority root

Use one selected work tree instead of repeating every precedence permutation.

```bash
TASK_DIR="$SMOKE_ROOT/task"
mkdir -p "$TASK_DIR"
printf 'relative ref\n' > "$TASK_DIR/notes.md"
uv run meridian work start smoke-task-dir --task-dir "$TASK_DIR"
uv run meridian spawn -a reviewer --harness codex -m "$DRY_RUN_MODEL" \
  -p "Use relative ref" --work smoke-task-dir -f notes.md --dry-run --json
```

- [ ] `task_cwd` and `reference_anchor` equal `$TASK_DIR`
- [ ] `task_cwd_source == "explicit-work-task-dir"`; `task_cwd_work_item == "smoke-task-dir"`
- [ ] Resolved reference points to `$TASK_DIR/notes.md`; authority remains the scratch project

```bash
KB_ROOT=$(uv run meridian context --json | uv run python -c 'import json,sys; print(json.load(sys.stdin)["kb_resolved"])')
mkdir -p "$KB_ROOT/domain"
printf 'kb ref\n' > "$KB_ROOT/domain/page.md"
uv run meridian spawn -a reviewer --harness codex -m "$DRY_RUN_MODEL" \
  -p "Use kb ref" -f kb:domain/page.md --dry-run --json
```

- [ ] Exit 0; JSON reports `task_cwd`, `reference_anchor`, `task_cwd_source`, not `authority_root`
- [ ] Without a work override, `task_cwd == "$SCRATCH"` and
      `task_cwd_source == "inherited-task-dir"` (setup pins `MERIDIAN_TASK_DIR`)

## Explicit project root from another CWD

```bash
CANONICAL="$SMOKE_ROOT/canonical"
WORKTREE="$SMOKE_ROOT/worktree"
mkdir -p "$CANONICAL/.mars/agents" "$WORKTREE"
cp "$SCRATCH/mars.toml" "$CANONICAL/"
printf '# Reviewer\n' > "$CANONICAL/.mars/agents/reviewer.md"
printf 'gitdir: %s\n' "$SMOKE_ROOT/missing-git-dir" > "$WORKTREE/.git"
(
  cd "$WORKTREE"
  MERIDIAN_PROJECT_DIR="$CANONICAL" MERIDIAN_TASK_DIR="$CANONICAL" \
    uv run --project "$SMOKE_ORIGINAL_CWD" meridian spawn -a reviewer \
      --harness codex -m "$DRY_RUN_MODEL" -p "Root targeting" --dry-run --json
)
```

- [ ] Exit 0; `resolved_authority.project_root` is `$CANONICAL`, not the worktree CWD
- [ ] `resolved_authority.project_root_source == "explicit"`

## Cleanup

```bash
smoke_cleanup
trap - EXIT
```
