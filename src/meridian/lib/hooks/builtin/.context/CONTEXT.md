# hooks/builtin/ — Implementation Contracts

## Architecture

Two files form one unit:

- **`git_autosync.py`** — sync cycle orchestration, git subprocess calls, hook protocol (`execute` → `_sync` → `_result`)
- **`autosync_store.py`** — artifact format ownership: conflict JSON schema, sync state JSON, AGENTS.md notice injection/removal. Stdlib-only, zero meridian imports.

They are designed together for potential extraction into a standalone plugin package. If you refactor one, check the other for interface assumptions.

## Contracts

### HookOutcome has no `conflict_detected` variant

`HookOutcome` from `meridian.plugin_api` does not include `"conflict_detected"`. When git_autosync detects a merge conflict, `HookResult` returns `outcome="skipped"` with `skip_reason="conflict_detected"`. The rich conflict state lives in `.meridian/autosync/` artifacts and the AGENTS.md notice block — not in `HookResult`.

**Test implication:** asserting `result.outcome == "conflict_detected"` will always fail. Assert `result.outcome == "skipped" and result.skip_reason == "conflict_detected"` instead.

### Conflict paths and type must be captured before `merge --abort`

In the merge conflict branch, `_get_conflict_paths()` and `_detect_conflict_type()` MUST be called and their results stored in locals before `git merge --abort` runs. After abort, the index is clean — unmerged entries are gone, and both functions return empty/`"content"` fallback values. The AGENTS.md notice and conflict record would show no affected files.

Correct ordering (as implemented):
```
conflict_paths = self._get_conflict_paths(clone_path)        # before abort
conflict_type  = self._detect_conflict_type(...)             # before abort
abort = self._run_git(clone_path, ["merge", "--abort"], ...) # then abort
write_conflict(..., paths=tuple(conflict_paths), ...)        # use captured values
```

### `_stash_excluded_files` must use `--include-untracked`

Removing `--include-untracked` from the stash push leaves untracked excluded files in the worktree. When the remote adds the same path, `git merge` fails with "would be overwritten by merge" — the exact failure mode the stash is designed to prevent.

### `_check_divergence` returns `None` on total failure, not `(0, 0)`

`None` means "cannot determine ahead/behind — skip with `divergence_check_failed`." A `(0, 0)` return in a failure path would silently skip merges and pushes. Every caller that receives `None` must emit `skip_reason="divergence_check_failed"` and return early. Do not default to `(0, 0)` in any error branch.

## Patterns

### `autosync_store` is the sole writer of `.meridian/autosync/` artifacts

`git_autosync.py` never constructs paths into `.meridian/autosync/` directly. All reads and writes go through `autosync_store` functions (`write_conflict`, `write_sync_state`, `append_conflict_notice`, etc.). Do not add `Path(clone_path) / ".meridian" / "autosync" / ...` path construction in `git_autosync.py`.

### Use `hook_event=`, not `event=`, in structlog calls

structlog reserves `event` as its own field. Passing `event=context.event_name` raises a "got multiple values for argument 'event'" collision error. All log calls that pass the lifecycle event name use the key `hook_event`.

### All execution paths return `HookResult` — never raise

`execute()` and `_sync()` catch `OSError` and `subprocess.SubprocessError` and convert them to `outcome="skipped"` outcomes. The dispatcher is fail-open for `observe` and `post` events. Unhandled exceptions would bypass this contract.

## Related

→ [../AGENTS.md](../AGENTS.md) — built-in hook mental model, import boundary rules, adding a built-in
→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — plugin API boundary, event classes, dispatch execution order
