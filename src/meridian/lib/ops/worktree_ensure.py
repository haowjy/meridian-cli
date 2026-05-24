"""Shared work-item worktree ensure helper used by commands and spawn flows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.config.workspace import resolve_workspace_snapshot
from meridian.lib.core.context import RuntimeContext
from meridian.lib.ops.runtime import runtime_context
from meridian.lib.ops.worktree_lifecycle import (
    default_worktree_branch,
    provision_for_start,
    recover_pending,
)
from meridian.lib.ops.worktree_ops import (
    detect_git_repo,
    managed_worktree_path,
    resolve_main_repo_root,
)
from meridian.lib.state import temp_worktree_store, work_store
from meridian.lib.state.work_store import WorkItem, WorktreeMetadata

WorktreeEnsureStatus = Literal[
    "already_available",
    "manual_available",
    "provisioned",
    "recovered",
    "would_use_existing",
    "would_provision",
    "temporary_available",
    "temporary_pending",
    "temporary_provisioned",
    "temporary_would_provision",
]


@dataclass(frozen=True)
class WorktreeEnsureResult:
    status: WorktreeEnsureStatus
    work_id: str | None
    metadata: WorktreeMetadata
    repo_root: Path
    canonical_path: Path
    warning: str | None = None

    @property
    def worktree_path(self) -> Path:
        return Path(self.metadata.path or self.canonical_path)

    @property
    def branch(self) -> str | None:
        return self.metadata.branch

    @property
    def worktree_name(self) -> str | None:
        return self.metadata.name

    @property
    def managed(self) -> bool:
        return self.metadata.managed

    @property
    def ensured(self) -> bool:
        return self.status in {
            "provisioned",
            "recovered",
            "would_provision",
            "would_use_existing",
            "temporary_provisioned",
            "temporary_would_provision",
        }


class WorktreeEnsureError(ValueError):
    """Raised when worktree ensure cannot safely proceed."""


def _looks_like_path_selector(selector: str) -> bool:
    return (
        selector.startswith((".", "~"))
        or "/" in selector
        or "\\" in selector
        or Path(selector).is_absolute()
    )


def _resolve_execution_repo_root(
    execution_cwd: Path | None,
    *,
    project_root: Path,
    dry_run: bool,
) -> Path | None:
    if execution_cwd is None:
        return None

    candidate = execution_cwd.expanduser().resolve()
    if dry_run:
        repo_root = _repo_root_without_git_commands(candidate)
    else:
        if not detect_git_repo(candidate):
            return None
        repo_root = resolve_main_repo_root(candidate)

    if repo_root is None:
        return None
    resolved_repo = repo_root.resolve()
    if resolved_repo == project_root.resolve():
        return None
    return resolved_repo


def _resolve_target_repo(
    project_root: Path,
    repo_selector: str | None,
    *,
    execution_cwd: Path | None = None,
    dry_run: bool = False,
) -> Path:
    if repo_selector is None or not repo_selector.strip():
        execution_repo = _resolve_execution_repo_root(
            execution_cwd,
            project_root=project_root,
            dry_run=dry_run,
        )
        if execution_repo is not None:
            return execution_repo

        snapshot = resolve_workspace_snapshot(project_root)
        if snapshot.status == "invalid":
            details = (
                "; ".join(finding.message for finding in snapshot.findings)
                or "invalid workspace config"
            )
            raise WorktreeEnsureError(
                "Cannot resolve implicit target repository because workspace config is invalid: "
                + details
            )
        authority_root = project_root.resolve()
        non_authority_aliases = sorted(
            root.name
            for root in snapshot.roots
            if root.enabled and root.exists and root.resolved_path.resolve() != authority_root
        )
        if non_authority_aliases:
            aliases = ", ".join(non_authority_aliases)
            raise WorktreeEnsureError(
                "Target repository is ambiguous from workspace config. "
                f"Pass --repo <path-or-alias>. Available aliases: {aliases}"
            )
        return authority_root

    token = repo_selector.strip()
    if _looks_like_path_selector(token):
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        return candidate.resolve()

    snapshot = resolve_workspace_snapshot(project_root)
    if snapshot.status == "invalid":
        details = (
            "; ".join(finding.message for finding in snapshot.findings)
            or "invalid workspace config"
        )
        raise WorktreeEnsureError(
            "Cannot resolve --repo alias because workspace config is invalid: " + details
        )
    for root in snapshot.roots:
        if root.name == token and root.enabled:
            if not root.exists:
                raise WorktreeEnsureError(
                    f"Workspace alias '{token}' points to a missing directory: "
                    f"{root.resolved_path.as_posix()}"
                )
            return root.resolved_path.resolve()

    candidate = (project_root / token).resolve()
    if candidate.exists():
        return candidate

    known_aliases = ", ".join(sorted(root.name for root in snapshot.roots if root.enabled))
    if known_aliases:
        raise WorktreeEnsureError(
            f"Unknown workspace alias '{token}'. Known aliases: {known_aliases}. "
            "Pass an existing path or configured alias."
        )
    raise WorktreeEnsureError(
        f"Unknown workspace alias '{token}'. Pass an existing path or configure "
        f"[workspace.{token}] in meridian.toml."
    )


def _repo_root_without_git_commands(target: Path) -> Path | None:
    start = target.resolve()
    for candidate in (start, *start.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return candidate
        if marker.is_file():
            try:
                contents = marker.read_text(encoding="utf-8").strip()
            except OSError:
                return candidate
            if contents.lower().startswith("gitdir:"):
                raw_gitdir = contents.split(":", 1)[1].strip()
                gitdir = Path(raw_gitdir)
                if not gitdir.is_absolute():
                    gitdir = (candidate / gitdir).resolve()
                for parent in (gitdir, *gitdir.parents):
                    if parent.name == ".git":
                        return parent.parent.resolve()
            return candidate
    return None


def _resolve_repo_root(target_repo: Path, *, dry_run: bool) -> Path:
    if dry_run:
        repo_root = _repo_root_without_git_commands(target_repo)
        if repo_root is None:
            raise WorktreeEnsureError(
                f"Cannot ensure worktree: '{target_repo}' is not inside a git repository."
            )
        return repo_root

    if not detect_git_repo(target_repo):
        raise WorktreeEnsureError(
            f"Cannot ensure worktree: '{target_repo}' is not inside a git repository."
        )
    repo_root = resolve_main_repo_root(target_repo)
    if repo_root is None:
        raise WorktreeEnsureError(
            f"Cannot determine git repository root from '{target_repo}'."
        )
    return repo_root


def _canonical_target(
    *,
    item: WorkItem,
    repo_root: Path,
) -> tuple[Path, WorktreeMetadata]:
    worktree_name = item.worktree.name or item.name
    branch = item.worktree_branch or default_worktree_branch(item.name)
    canonical_path = managed_worktree_path(repo_root, worktree_name)
    metadata = item.worktree.model_copy(
        update={
            "path": canonical_path.as_posix(),
            "branch": branch,
            "repo_path": repo_root.as_posix(),
            "name": worktree_name,
            "pending": False,
            "managed": True,
        }
    )
    return canonical_path, metadata


def _stored_managed_repo_root(item: WorkItem) -> Path | None:
    """Return the recorded managed repo root, if this item already has one."""
    if not item.worktree_managed:
        return None
    if not item.worktree_repo_path or not item.worktree.name:
        raise WorktreeEnsureError(
            f"Managed worktree metadata for '{item.name}' is missing canonical repo/name "
            "fields. Clear or migrate the assignment before ensuring."
        )
    return Path(item.worktree_repo_path).expanduser().resolve()


def _temporary_key(ctx: RuntimeContext | None = None) -> str:
    resolved = runtime_context(ctx)
    if resolved.spawn_id is not None:
        return f"spawn-{resolved.spawn_id}"
    if resolved.chat_id.strip():
        return f"chat-{resolved.chat_id.strip()}"
    return "default"


def _temporary_name(*, project_root: Path, key: str) -> str:
    candidate = work_store.slugify(f"temp-{key}")
    if candidate:
        return candidate
    fallback = work_store.slugify(f"temp-{project_root.name}")
    return fallback or "temp-worktree"


def _persist_metadata(project_state_dir: Path, work_id: str, metadata: WorktreeMetadata) -> None:
    work_store.update_work_item_worktree(
        project_state_dir,
        work_id,
        path=metadata.path,
        branch=metadata.branch,
        repo_path=metadata.repo_path,
        name=metadata.name,
        pending=metadata.pending,
        managed=metadata.managed,
    )


def ensure_work_item_worktree(
    *,
    project_root: Path,
    project_state_dir: Path,
    work_id: str,
    target_repo: str | None = None,
    execution_cwd: Path | None = None,
    dry_run: bool = False,
) -> WorktreeEnsureResult:
    """Ensure the selected work item's managed worktree exists at canonical path."""

    item = work_store.get_work_item(project_state_dir, work_id)
    if item is None:
        raise WorktreeEnsureError(f"Work item '{work_id}' not found")
    if item.status == "done":
        raise WorktreeEnsureError(
            f"Work item '{work_id}' is archived. Reopen it before ensuring a worktree."
        )

    stored_repo_root = _stored_managed_repo_root(item)
    if item.worktree_path is not None:
        existing_path = Path(item.worktree_path).expanduser()
        if existing_path.is_dir():
            resolved_path = existing_path.resolve()
            if not item.worktree_managed:
                return WorktreeEnsureResult(
                    status="manual_available",
                    work_id=work_id,
                    metadata=item.worktree,
                    repo_root=project_root.resolve(),
                    canonical_path=resolved_path,
                )
            if stored_repo_root is None:
                raise WorktreeEnsureError(
                    f"Managed worktree metadata for '{work_id}' is missing canonical repo/name "
                    "fields. Clear or migrate the assignment before ensuring."
                )
            canonical_path, canonical_metadata = _canonical_target(
                item=item,
                repo_root=stored_repo_root,
            )
            if resolved_path != canonical_path:
                raise WorktreeEnsureError(
                    f"Managed worktree path drift for '{work_id}': "
                    f"'{resolved_path}' is non-canonical; expected '{canonical_path}'. "
                    "Clear or migrate the assignment before ensuring."
                )
            was_pending = item.worktree_pending
            if not dry_run and (
                was_pending or item.worktree_branch != canonical_metadata.branch
            ):
                _persist_metadata(
                    project_state_dir,
                    work_id,
                    canonical_metadata.model_copy(
                        update={"path": item.worktree_path, "pending": False}
                    )
                )
                item = work_store.get_work_item(project_state_dir, work_id) or item
            return WorktreeEnsureResult(
                status=(
                    "would_use_existing"
                    if dry_run
                    else ("recovered" if was_pending else "already_available")
                ),
                work_id=work_id,
                metadata=item.worktree,
                repo_root=stored_repo_root,
                canonical_path=canonical_path,
                warning=(
                    "Pending marker present; runtime ensure will heal metadata."
                    if dry_run and was_pending
                    else None
                ),
            )
        elif not item.worktree_managed:
            raise WorktreeEnsureError(
                f"Work item '{work_id}' has a manual worktree assignment that is missing: "
                f"'{item.worktree_path}'. Restore it, clear the assignment with "
                f"`meridian work clear-worktree {work_id}`, or use --no-worktree."
            )
        elif stored_repo_root is not None:
            canonical_path, _canonical_metadata = _canonical_target(
                item=item,
                repo_root=stored_repo_root,
            )
            if existing_path.resolve() != canonical_path:
                raise WorktreeEnsureError(
                    f"Managed worktree path drift for '{work_id}': "
                    f"'{existing_path.resolve()}' is non-canonical; expected "
                    f"'{canonical_path}'. Clear or migrate the assignment before ensuring."
                )
        else:
            raise WorktreeEnsureError(
                f"Managed worktree metadata for '{work_id}' is missing canonical repo/name "
                "fields. Clear or migrate the assignment before ensuring."
            )

    if stored_repo_root is not None:
        repo_root = _resolve_repo_root(stored_repo_root, dry_run=dry_run)
    else:
        target_repo_path = _resolve_target_repo(
            project_root,
            target_repo,
            execution_cwd=execution_cwd,
            dry_run=dry_run,
        )
        repo_root = _resolve_repo_root(target_repo_path, dry_run=dry_run)
    canonical_path, canonical_metadata = _canonical_target(
        item=item,
        repo_root=repo_root,
    )

    if dry_run:
        if (
            item.worktree_path is not None
            and item.worktree_managed
            and Path(item.worktree_path).is_dir()
        ):
            return WorktreeEnsureResult(
                status="would_use_existing",
                work_id=work_id,
                metadata=canonical_metadata,
                repo_root=repo_root,
                canonical_path=canonical_path,
            )
        warning = None
        if item.worktree_pending:
            warning = (
                "Pending marker present; runtime ensure will run recovery before provisioning."
            )
        return WorktreeEnsureResult(
            status="would_provision",
            work_id=work_id,
            metadata=canonical_metadata,
            repo_root=repo_root,
            canonical_path=canonical_path,
            warning=warning,
        )

    recovered_pending = False
    if item.worktree_pending:
        recovered = recover_pending(project_root, item)
        recovered_pending = recovered.status == "healed"
        _persist_metadata(project_state_dir, work_id, recovered.metadata)
        item = work_store.get_work_item(project_state_dir, work_id) or item

    if (
        item.worktree_path is not None
        and item.worktree_managed
        and Path(item.worktree_path).is_dir()
    ):
        if (
            item.worktree_repo_path != canonical_metadata.repo_path
            or item.worktree.name != canonical_metadata.name
            or item.worktree_branch != canonical_metadata.branch
        ):
            _persist_metadata(
                project_state_dir,
                work_id,
                canonical_metadata.model_copy(
                    update={"path": item.worktree_path, "pending": False}
                ),
            )
            item = work_store.get_work_item(project_state_dir, work_id) or item
        return WorktreeEnsureResult(
            status="recovered" if recovered_pending else "already_available",
            work_id=work_id,
            metadata=item.worktree,
            repo_root=repo_root,
            canonical_path=canonical_path,
        )

    previous = item.worktree
    pending_metadata = canonical_metadata.model_copy(update={"pending": True})
    _persist_metadata(project_state_dir, work_id, pending_metadata)

    try:
        provisioned = provision_for_start(
            repo_root,
            canonical_metadata.name or item.name,
            existing=canonical_metadata,
        )
    except Exception:
        _persist_metadata(
            project_state_dir,
            work_id,
            previous.model_copy(update={"pending": False}),
        )
        raise

    if provisioned.status == "skipped_not_git_repo":
        _persist_metadata(
            project_state_dir,
            work_id,
            previous.model_copy(update={"pending": False}),
        )
        raise WorktreeEnsureError(
            f"Cannot ensure worktree for '{work_id}': '{repo_root}' is not inside a git repository."
        )

    ensured_metadata = canonical_metadata.model_copy(
        update={
            "path": provisioned.metadata.path,
            "branch": provisioned.metadata.branch,
            "pending": False,
            "managed": True,
        }
    )
    _persist_metadata(project_state_dir, work_id, ensured_metadata)

    return WorktreeEnsureResult(
        status=(
            "recovered"
            if (
                recovered_pending
                or previous.managed
                or previous.path is not None
                or previous.pending
            )
            else "provisioned"
        ),
        work_id=work_id,
        metadata=ensured_metadata,
        repo_root=repo_root,
        canonical_path=canonical_path,
    )


def ensure_temporary_worktree(
    *,
    project_root: Path,
    runtime_root: Path,
    ctx: RuntimeContext | None = None,
    target_repo: str | None = None,
    execution_cwd: Path | None = None,
    dry_run: bool = False,
) -> WorktreeEnsureResult:
    """Ensure a managed temporary worktree for current session/task."""

    key = _temporary_key(ctx)
    record = temp_worktree_store.get_temporary_worktree(runtime_root, key)
    requested_repo_selector = target_repo
    if record is not None:
        repo_root = Path(record.repo_path).expanduser().resolve()
        if requested_repo_selector is not None:
            requested_repo = _resolve_target_repo(
                project_root,
                requested_repo_selector,
                execution_cwd=execution_cwd,
                dry_run=dry_run,
            )
            requested_root = _resolve_repo_root(requested_repo, dry_run=dry_run)
            if requested_root != repo_root:
                raise WorktreeEnsureError(
                    "Tracked temporary worktree belongs to a different target repository.\n"
                    f"  tracked:   {repo_root}\n"
                    f"  requested: {requested_root}\n"
                    "Use the tracked repository, clear the temporary record, or choose a "
                    "different session/task context."
                )
        repo_root = _resolve_repo_root(repo_root, dry_run=dry_run)
    else:
        target_repo_path = _resolve_target_repo(
            project_root,
            requested_repo_selector,
            execution_cwd=execution_cwd,
            dry_run=dry_run,
        )
        repo_root = _resolve_repo_root(target_repo_path, dry_run=dry_run)
    worktree_name = record.worktree_name if record is not None else _temporary_name(
        project_root=project_root,
        key=key,
    )
    branch = record.branch if record is not None else default_worktree_branch(worktree_name)
    canonical_path = managed_worktree_path(repo_root, worktree_name).resolve()
    metadata = WorktreeMetadata(
        path=canonical_path.as_posix(),
        branch=branch,
        repo_path=repo_root.as_posix(),
        name=worktree_name,
        pending=record.status == "pending" if record is not None else False,
        managed=True,
    )
    warning: str | None = None
    if record is not None:
        recorded_path = Path(record.worktree_path).expanduser().resolve()
        if recorded_path != canonical_path:
            raise WorktreeEnsureError(
                "Tracked temporary worktree path is non-canonical for target repository.\n"
                f"  expected: {canonical_path}\n"
                f"  actual:   {recorded_path}\n"
                "Pass an explicit --repo matching the tracked repo "
                "or clear/recreate the temp worktree."
            )
        if recorded_path.is_dir():
            if record.status == "pending" and not dry_run:
                temp_worktree_store.put_temporary_worktree(
                    runtime_root,
                    key=key,
                    repo_path=repo_root.as_posix(),
                    worktree_name=worktree_name,
                    worktree_path=recorded_path.as_posix(),
                    branch=branch,
                    status="ready",
                    managed=True,
                )
                warning = f"Recovered interrupted temporary worktree at {recorded_path}"
            return WorktreeEnsureResult(
                status="temporary_available",
                work_id=None,
                metadata=metadata.model_copy(
                    update={"path": recorded_path.as_posix(), "pending": False}
                ),
                repo_root=repo_root,
                canonical_path=canonical_path,
                warning=warning,
            )
    if dry_run:
        return WorktreeEnsureResult(
            status="temporary_would_provision",
            work_id=None,
            metadata=metadata,
            repo_root=repo_root,
            canonical_path=canonical_path,
            warning=(
                f"Dry-run: would recover pending temporary worktree at {canonical_path}"
                if record is not None and record.status == "pending"
                else f"Dry-run: would ensure temporary worktree at {canonical_path}"
            ),
        )

    temp_worktree_store.put_temporary_worktree(
        runtime_root,
        key=key,
        repo_path=repo_root.as_posix(),
        worktree_name=worktree_name,
        worktree_path=canonical_path.as_posix(),
        branch=branch,
        status="pending",
        managed=True,
    )
    provisioned = provision_for_start(
        repo_root,
        worktree_name,
        existing=metadata,
    )
    if provisioned.status == "skipped_not_git_repo":
        raise WorktreeEnsureError(
            f"Cannot ensure temporary worktree: '{repo_root}' is not inside a git repository."
        )
    resolved_path = Path(provisioned.metadata.path or canonical_path).resolve()
    resolved_branch = provisioned.metadata.branch or branch
    temp_worktree_store.put_temporary_worktree(
        runtime_root,
        key=key,
        repo_path=repo_root.as_posix(),
        worktree_name=worktree_name,
        worktree_path=resolved_path.as_posix(),
        branch=resolved_branch,
        status="ready",
        managed=True,
    )
    return WorktreeEnsureResult(
        status="temporary_provisioned",
        work_id=None,
        metadata=metadata.model_copy(
            update={
                "path": resolved_path.as_posix(),
                "branch": resolved_branch,
                "pending": False,
            }
        ),
        repo_root=repo_root,
        canonical_path=canonical_path,
    )


def get_temporary_worktree_status(
    *,
    runtime_root: Path,
    ctx: RuntimeContext | None = None,
) -> WorktreeEnsureResult | None:
    key = _temporary_key(ctx)
    record = temp_worktree_store.get_temporary_worktree(runtime_root, key)
    if record is None:
        return None
    worktree_path = Path(record.worktree_path).expanduser().resolve()
    repo_root = Path(record.repo_path).expanduser().resolve()
    canonical_path = managed_worktree_path(repo_root, record.worktree_name).resolve()
    pending = record.status == "pending"
    warning: str | None = None
    if pending and worktree_path.is_dir():
        temp_worktree_store.put_temporary_worktree(
            runtime_root,
            key=key,
            repo_path=repo_root.as_posix(),
            worktree_name=record.worktree_name,
            worktree_path=worktree_path.as_posix(),
            branch=record.branch,
            status="ready",
            managed=record.managed,
        )
        pending = False
        warning = f"Recovered interrupted temporary worktree at {worktree_path}"
    elif pending:
        warning = (
            f"Temporary worktree provisioning was interrupted before {worktree_path} "
            "became available. Run `meridian work worktree --ensure` to recover it."
        )
    return WorktreeEnsureResult(
        status="temporary_pending" if pending else "temporary_available",
        work_id=None,
        metadata=WorktreeMetadata(
            path=worktree_path.as_posix(),
            branch=record.branch,
            repo_path=repo_root.as_posix(),
            name=record.worktree_name,
            pending=pending,
            managed=record.managed,
        ),
        repo_root=repo_root,
        canonical_path=canonical_path,
        warning=warning,
    )


__all__ = [
    "WorktreeEnsureError",
    "WorktreeEnsureResult",
    "ensure_temporary_worktree",
    "ensure_work_item_worktree",
    "get_temporary_worktree_status",
]
