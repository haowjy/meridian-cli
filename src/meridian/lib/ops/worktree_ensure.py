"""Shared work-item worktree ensure helper used by commands and spawn flows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.config.settings import load_config
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
    resolve_main_repo_root,
    resolve_worktree_path,
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


def _resolve_repo_selector(project_root: Path, repo_selector: str | None) -> Path:
    if repo_selector is None or not repo_selector.strip():
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
    canonical_path = resolve_worktree_path(repo_root, worktree_name, worktree_base=None)
    metadata = item.worktree.model_copy(
        update={
            "path": str(canonical_path),
            "branch": branch,
            "repo_path": str(repo_root),
            "name": worktree_name,
            "pending": False,
            "managed": True,
        }
    )
    return canonical_path, metadata


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
    repo_selector: str | None = None,
    repo: str | None = None,
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

    if item.worktree_path is not None and not item.worktree_managed:
        manual_path = Path(item.worktree_path)
        if manual_path.is_dir():
            target_repo = _resolve_repo_selector(project_root, repo_selector or repo)
            repo_root = _resolve_repo_root(target_repo, dry_run=dry_run)
            canonical_path, _canonical_metadata = _canonical_target(
                item=item,
                repo_root=repo_root,
            )
            return WorktreeEnsureResult(
                status="manual_available",
                work_id=work_id,
                metadata=item.worktree,
                repo_root=repo_root,
                canonical_path=canonical_path,
            )
        raise WorktreeEnsureError(
            f"Work item '{work_id}' has a manual worktree assignment that is missing: "
            f"'{item.worktree_path}'. Restore it, clear the assignment with "
            f"`meridian work clear-worktree {work_id}`, or use --no-worktree."
        )

    target_repo = _resolve_repo_selector(project_root, repo_selector or repo)
    repo_root = _resolve_repo_root(target_repo, dry_run=dry_run)
    canonical_path, canonical_metadata = _canonical_target(
        item=item,
        repo_root=repo_root,
    )

    if item.worktree_path is not None and item.worktree_managed:
        existing_path = Path(item.worktree_path).expanduser().resolve()
        if existing_path != canonical_path:
            raise WorktreeEnsureError(
                f"Managed worktree path drift for '{work_id}': '{existing_path}' is non-canonical; "
                f"expected '{canonical_path}'. Clear or migrate the assignment before ensuring."
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
            # Canonical target path is pinned through `existing.path`; config base is ignored.
            # Keep using a local config snapshot for branch/other defaults.
            load_config(project_root).work,
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
            if (recovered_pending or previous.path is not None or previous.pending)
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
    repo_selector: str | None = None,
    repo: str | None = None,
    dry_run: bool = False,
) -> WorktreeEnsureResult:
    """Ensure a managed temporary worktree for current session/task."""

    key = _temporary_key(ctx)
    target_repo = _resolve_repo_selector(project_root, repo_selector or repo)
    repo_root = _resolve_repo_root(target_repo, dry_run=dry_run)
    record = temp_worktree_store.get_temporary_worktree(runtime_root, key)
    worktree_name = record.worktree_name if record is not None else _temporary_name(
        project_root=project_root,
        key=key,
    )
    branch = record.branch if record is not None else default_worktree_branch(worktree_name)
    canonical_path = resolve_worktree_path(repo_root, worktree_name, worktree_base=None).resolve()
    metadata = WorktreeMetadata(
        path=canonical_path.as_posix(),
        branch=branch,
        repo_path=repo_root.as_posix(),
        name=worktree_name,
        pending=False,
        managed=True,
    )
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
            return WorktreeEnsureResult(
                status="temporary_available",
                work_id=None,
                metadata=metadata.model_copy(update={"path": recorded_path.as_posix()}),
                repo_root=repo_root,
                canonical_path=canonical_path,
            )
    if dry_run:
        return WorktreeEnsureResult(
            status="temporary_would_provision",
            work_id=None,
            metadata=metadata,
            repo_root=repo_root,
            canonical_path=canonical_path,
            warning=f"Dry-run: would ensure temporary worktree at {canonical_path}",
        )

    provisioned = provision_for_start(
        repo_root,
        worktree_name,
        load_config(project_root).work,
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
        managed=True,
    )
    return WorktreeEnsureResult(
        status="temporary_provisioned",
        work_id=None,
        metadata=metadata.model_copy(
            update={"path": resolved_path.as_posix(), "branch": resolved_branch}
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
    canonical_path = resolve_worktree_path(
        repo_root,
        record.worktree_name,
        worktree_base=None,
    ).resolve()
    return WorktreeEnsureResult(
        status="temporary_available",
        work_id=None,
        metadata=WorktreeMetadata(
            path=worktree_path.as_posix(),
            branch=record.branch,
            repo_path=repo_root.as_posix(),
            name=record.worktree_name,
            pending=False,
            managed=record.managed,
        ),
        repo_root=repo_root,
        canonical_path=canonical_path,
    )


__all__ = [
    "WorktreeEnsureError",
    "WorktreeEnsureResult",
    "ensure_temporary_worktree",
    "ensure_work_item_worktree",
    "get_temporary_worktree_status",
]
