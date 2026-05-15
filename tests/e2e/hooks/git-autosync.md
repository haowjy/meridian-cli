# Hook: git-autosync

Validate the built-in `git-autosync` hook in an isolated git repository with a real bare remote.

## Setup

```bash
export REPO_ROOT=/abs/path/to/meridian-cli
export E2E_REPO="$(mktemp -d /tmp/meridian-git-autosync.XXXXXX)"
export E2E_REMOTE="$E2E_REPO/remote.git"
export E2E_SEED="$E2E_REPO/seed"
export E2E_WORK="$E2E_REPO/work"

git init --bare "$E2E_REMOTE"
git init "$E2E_SEED"
git -C "$E2E_SEED" config user.email "autosync-e2e@example.com"
git -C "$E2E_SEED" config user.name "Autosync E2E"
printf "seed\n" > "$E2E_SEED/keep.txt"
git -C "$E2E_SEED" add -A
git -C "$E2E_SEED" commit -m "seed"
git -C "$E2E_SEED" remote add origin "$E2E_REMOTE"
git -C "$E2E_SEED" push -u origin HEAD
git clone "$E2E_REMOTE" "$E2E_WORK"
git -C "$E2E_WORK" config user.email "autosync-e2e@example.com"
git -C "$E2E_WORK" config user.name "Autosync E2E"

cd "$REPO_ROOT"
test -d "$E2E_WORK/.git" && echo "PASS: autosync e2e repo ready" || echo "FAIL: autosync e2e repo setup failed"
```

### AUTOSYNC-1. Built-in metadata is registered [CRITICAL]

```bash
uv run python - <<'PY'
from meridian.lib.hooks.builtin import BUILTIN_HOOKS

hook = BUILTIN_HOOKS.get("git-autosync")
assert hook is not None
assert hook.default_events == ("spawn.start", "spawn.finalized", "work.started", "work.done")
assert hook.requirements == ("git",)
print("PASS: git-autosync metadata is registered")
PY
```

### AUTOSYNC-2. Sync sequence commits and pushes changes [CRITICAL]

```bash
uv run python - <<'PY'
from uuid import uuid4

from meridian.lib.hooks.builtin.git_autosync import GitAutosync
from meridian.lib.hooks.types import Hook, HookContext

import os
from pathlib import Path

work = Path(os.environ["E2E_WORK"])
(work / "keep.txt").write_text("changed by autosync\n", encoding="utf-8")
(work / "new.txt").write_text("new file\n", encoding="utf-8")

hook = Hook(
    name="git-autosync",
    event="work.done",
    source="project",
    builtin="git-autosync",
    repo=os.environ["E2E_REMOTE"],
)
context = HookContext(
    event_name="work.done",
    event_id=uuid4(),
    timestamp="2026-04-20T00:00:00+00:00",
    project_root=str(work),
    runtime_root=str(work / ".meridian"),
    work_id="w1",
    work_dir=str(work),
)
result = GitAutosync().execute(context, hook)
assert result.success is True
assert result.outcome == "success"
print("PASS: git-autosync executed successfully")
PY

git -C "$E2E_WORK" log -1 --pretty=%s | grep -q '^autosync: ' && \
echo "PASS: autosync commit created" || echo "FAIL: autosync commit not created"
```

### AUTOSYNC-3. Exclude patterns keep files out of autosync commit [IMPORTANT]

Note: `exclude` only filters newly staged untracked files. It does not block already tracked file modifications from syncing.

```bash
uv run python - <<'PY'
from uuid import uuid4

from meridian.lib.hooks.builtin.git_autosync import GitAutosync
from meridian.lib.hooks.types import Hook, HookContext

import os
from pathlib import Path

work = Path(os.environ["E2E_WORK"])
(work / "keep.txt").write_text("second include\n", encoding="utf-8")
(work / "debug.log").write_text("should be excluded\n", encoding="utf-8")
(work / "tmp").mkdir(exist_ok=True)
(work / "tmp" / "cache.txt").write_text("excluded dir entry\n", encoding="utf-8")

hook = Hook(
    name="git-autosync",
    event="work.done",
    source="project",
    builtin="git-autosync",
    repo=os.environ["E2E_REMOTE"],
    exclude=("*.log", "tmp/"),
)
context = HookContext(
    event_name="work.done",
    event_id=uuid4(),
    timestamp="2026-04-20T00:00:00+00:00",
    project_root=str(work),
    runtime_root=str(work / ".meridian"),
    work_id="w2",
    work_dir=str(work),
)
result = GitAutosync().execute(context, hook)
assert result.success is True
print("PASS: autosync with excludes executed")
PY

git -C "$E2E_WORK" show --pretty=format: --name-only HEAD > /tmp/meridian-autosync-last-files.txt && \
grep -q '^keep.txt$' /tmp/meridian-autosync-last-files.txt && \
! grep -q '^debug.log$' /tmp/meridian-autosync-last-files.txt && \
! grep -q '^tmp/cache.txt$' /tmp/meridian-autosync-last-files.txt && \
echo "PASS: exclude patterns respected" || echo "FAIL: excluded paths were committed"
```

### AUTOSYNC-4. Conflict path aborts merge, records metadata, and skips sync [IMPORTANT]

Expected behavior: autosync captures conflict paths, aborts the merge, records
`.meridian/autosync/conflicts/*.json`, and (when `AGENTS.md` exists) appends a notice block.

```bash
export E2E_OTHER="$E2E_REPO/other"
git clone "$E2E_REMOTE" "$E2E_OTHER"
git -C "$E2E_OTHER" config user.email "autosync-e2e@example.com"
git -C "$E2E_OTHER" config user.name "Autosync E2E"
printf "remote change\n" > "$E2E_OTHER/keep.txt"
git -C "$E2E_OTHER" add -A
git -C "$E2E_OTHER" commit -m "remote conflict change"
git -C "$E2E_OTHER" push origin HEAD

printf "local conflict change\n" > "$E2E_WORK/keep.txt"
git -C "$E2E_WORK" add -A
git -C "$E2E_WORK" commit -m "local conflict change"

uv run python - <<'PY'
from uuid import uuid4

from meridian.lib.hooks.builtin.git_autosync import GitAutosync
from meridian.lib.hooks.types import Hook, HookContext

import os
from pathlib import Path

work = Path(os.environ["E2E_WORK"])
hook = Hook(
    name="git-autosync",
    event="work.done",
    source="project",
    builtin="git-autosync",
    repo=os.environ["E2E_REMOTE"],
)
context = HookContext(
    event_name="work.done",
    event_id=uuid4(),
    timestamp="2026-04-20T00:00:00+00:00",
    project_root=str(work),
    runtime_root=str(work / ".meridian"),
    work_id="w3",
    work_dir=str(work),
)
result = GitAutosync().execute(context, hook)
assert result.success is True
assert result.skipped is True
assert result.skip_reason == "conflict_detected"
print("PASS: conflict path skipped with conflict_detected")
PY

test ! -f "$E2E_WORK/.git/MERGE_HEAD" && \
test -f "$E2E_WORK/.meridian/autosync/state.json" && \
test -d "$E2E_WORK/.meridian/autosync/conflicts" && \
ls "$E2E_WORK/.meridian/autosync/conflicts/"*.json >/dev/null 2>&1 && \
echo "PASS: merge aborted and conflict metadata recorded" || echo "FAIL: conflict metadata flow failed"
```

### AUTOSYNC-5. No-op run skips with `nothing_to_sync` [IMPORTANT]

```bash
export E2E_NOOP="$E2E_REPO/noop"
git clone "$E2E_REMOTE" "$E2E_NOOP"
git -C "$E2E_NOOP" config user.email "autosync-e2e@example.com"
git -C "$E2E_NOOP" config user.name "Autosync E2E"

uv run python - <<'PY'
from uuid import uuid4

from meridian.lib.hooks.builtin.git_autosync import GitAutosync
from meridian.lib.hooks.types import Hook, HookContext

import os
from pathlib import Path

work = Path(os.environ["E2E_NOOP"])
hook = Hook(
    name="git-autosync",
    event="work.done",
    source="project",
    builtin="git-autosync",
    repo=os.environ["E2E_REMOTE"],
)
context = HookContext(
    event_name="work.done",
    event_id=uuid4(),
    timestamp="2026-04-20T00:00:00+00:00",
    project_root=str(work),
    runtime_root=str(work / ".meridian"),
    work_id="w4",
    work_dir=str(work),
)
result = GitAutosync().execute(context, hook)
assert result.success is True
assert result.skipped is True
assert result.skip_reason == "nothing_to_sync"
print("PASS: clean tree skipped with nothing_to_sync")
PY
```

### AUTOSYNC-6. Requirement check reports missing git CLI [IMPORTANT]

```bash
uv run python - <<'PY'
import os

from meridian.lib.hooks.builtin.git_autosync import GitAutosync

original_path = os.environ.get("PATH", "")
try:
    os.environ["PATH"] = ""
    ok, error = GitAutosync().check_requirements()
finally:
    os.environ["PATH"] = original_path

assert ok is False
assert error is not None
assert "not found" in error.lower()
print("PASS: missing git CLI requirement is detected")
PY
```

### AUTOSYNC-7. Non-git directory skips with `not_git_repository` [IMPORTANT]

```bash
export E2E_NOT_GIT="$E2E_REPO/not-git"
mkdir -p "$E2E_NOT_GIT"
printf "plain directory\n" > "$E2E_NOT_GIT/README.txt"

uv run python - <<'PY'
from uuid import uuid4

from meridian.lib.hooks.builtin.git_autosync import GitAutosync
from meridian.lib.hooks.types import Hook, HookContext

import os
from pathlib import Path

work = Path(os.environ["E2E_NOT_GIT"])
hook = Hook(
    name="git-autosync",
    event="work.done",
    source="project",
    builtin="git-autosync",
    repo=os.environ["E2E_REMOTE"],
)
context = HookContext(
    event_name="work.done",
    event_id=uuid4(),
    timestamp="2026-04-20T00:00:00+00:00",
    project_root=str(work),
    runtime_root=str(work / ".meridian"),
    work_id="w5",
    work_dir=str(work),
)
result = GitAutosync().execute(context, hook)
assert result.success is True
assert result.skipped is True
assert result.skip_reason == "not_git_repository"
print("PASS: non-git work dir skipped with not_git_repository")
PY
```

### AUTOSYNC-8. Worktree scenario placeholder [OPTIONAL]

TODO: add a linked-worktree autosync scenario that verifies clean-skip and push behavior when the work dir is a Git worktree checkout rather than the main checkout path.
