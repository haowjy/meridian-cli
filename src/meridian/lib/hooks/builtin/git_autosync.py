"""Built-in git autosync hook implementation.

This hook uses meridian.plugin_api exclusively - it does NOT import from
meridian.lib.* for any functionality. This validates the plugin API surface.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import structlog

from meridian.lib.hooks.builtin.autosync_store import (
    AUTOSYNC_IGNORE_PATTERNS,
    ConflictRecord,
    append_conflict_notice,
    generate_conflict_id,
    has_unresolved_conflict,
    write_conflict,
    write_sync_state,
)
from meridian.plugin_api import (
    Hook,
    HookContext,
    HookOutcome,
    HookResult,
)
from meridian.plugin_api.fs import file_lock
from meridian.plugin_api.git import normalize_repo_url, resolve_clone_path
from meridian.plugin_api.state import get_user_home

logger = structlog.get_logger(__name__)

_REQUIREMENTS_TIMEOUT_SECS = 5
_CLONE_TIMEOUT_SECS = 300
_REMOTE_TIMEOUT_SECS = 10
_FETCH_TIMEOUT_SECS = 60
_MERGE_TIMEOUT_SECS = 60
_MERGE_ABORT_TIMEOUT_SECS = 30
_ADD_TIMEOUT_SECS = 30
_COMMIT_TIMEOUT_SECS = 30
_PUSH_TIMEOUT_SECS = 60
_STASH_TIMEOUT_SECS = 30
_DIFF_TIMEOUT_SECS = 30
_MAX_ERROR_CHARS = 500
_LOCK_TIMEOUT_SECS = 60.0


@dataclass(frozen=True)
class _SyncOutcome:
    outcome: HookOutcome
    success: bool
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None
    conflict_id: str | None = None
    conflict_paths: tuple[str, ...] | None = None
    files_changed: dict[str, int] | None = None


class GitAutosync:
    """Automatically sync a repository to its git remote."""

    name: str = "git-autosync"
    requirements: tuple[str, ...] = ("git",)

    # Default events - runs on all four lifecycle events
    default_events: tuple[str, ...] = (
        "spawn.start",
        "spawn.finalized",
        "work.started",
        "work.done",
    )
    # No default interval - runs on every event
    default_interval: str | None = None

    def check_requirements(self) -> tuple[bool, str | None]:
        """Return whether git is available in the environment."""

        git_bin = shutil.which("git")
        if git_bin is None:
            return False, "git CLI not found in PATH."

        try:
            result = subprocess.run(
                [git_bin, "--version"],
                capture_output=True,
                text=True,
                timeout=_REQUIREMENTS_TIMEOUT_SECS,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except FileNotFoundError:
            return False, "git CLI not found in PATH."
        except subprocess.TimeoutExpired:
            return False, "git --version timed out."
        except OSError as exc:
            return False, f"git --version failed: {exc}"

        if result.returncode != 0:
            return False, "git --version returned a non-zero exit code."
        return True, None

    def execute(self, context: HookContext, config: Hook) -> HookResult:
        """Execute git autosync for one hook invocation."""

        start = time.monotonic()
        remote_url = self._resolve_remote_url(config)

        # No remote → local-only mode (git init + add + commit, no push)
        if remote_url is None:
            return self._execute_local(context, config, start)

        # Resolve clone path using plugin_api
        clone_path = resolve_clone_path(remote_url)

        # Lock identity is clone-target based so URL aliases for the same clone path
        # serialize correctly.
        clone_path_hash = hashlib.sha256(str(clone_path.resolve()).encode("utf-8")).hexdigest()[:16]
        lock_file_path = get_user_home() / "locks" / f"clone-{clone_path_hash}.lock"
        try:
            with file_lock(lock_file_path, timeout=_LOCK_TIMEOUT_SECS):
                return self._execute_with_lock(context, config, clone_path, start)
        except TimeoutError as exc:
            logger.warning(
                "git_autosync_lock_timeout",
                repo=remote_url,
                clone_path=str(clone_path),
                error=str(exc),
            )
            return self._result(
                config,
                context,
                _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="lock_timeout",
                    error=str(exc),
                ),
                start=start,
                clone_path=str(clone_path),
                context_name=remote_url,
            )
        except (PermissionError, OSError) as exc:
            logger.warning(
                "git_autosync_lock_permission_error",
                repo=remote_url,
                clone_path=str(clone_path),
                error=str(exc),
            )
            return self._result(
                config,
                context,
                _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="lock_permission_error",
                    error=str(exc),
                ),
                start=start,
                clone_path=str(clone_path),
                context_name=remote_url,
            )

    def _execute_with_lock(
        self,
        context: HookContext,
        config: Hook,
        clone_path: Path,
        start: float,
    ) -> HookResult:
        """Execute sync workflow while holding the clone lock."""

        remote_url = self._resolve_remote_url(config)
        if remote_url is None:
            return self._result(
                config,
                context,
                _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="missing_repo",
                    error="Hook config does not include repo.",
                ),
                start=start,
                clone_path=str(clone_path),
            )

        # Ensure clone exists
        clone_ok, clone_error = self._ensure_clone(remote_url, clone_path)
        if not clone_ok:
            logger.warning(
                "git_autosync_clone_failed",
                repo=remote_url,
                clone_path=str(clone_path),
                error=clone_error,
            )
            return self._result(
                config,
                context,
                _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="clone_failed",
                    error=clone_error,
                ),
                start=start,
                clone_path=str(clone_path),
                context_name=remote_url,
            )

        try:
            conflict_policy = config.options.get("conflict_policy", "abort")
            outcome = self._sync(
                str(clone_path),
                config.exclude,
                conflict_policy,
                context=context,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(
                "git_autosync_runtime_error",
                clone_path=str(clone_path),
                error=str(exc),
            )
            outcome = _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="git_runtime_error",
                error=str(exc),
            )
        return self._result(
            config,
            context,
            outcome,
            start=start,
            clone_path=str(clone_path),
            context_name=remote_url,
        )

    def _execute_local(
        self,
        context: HookContext,
        config: Hook,
        start: float,
    ) -> HookResult:
        """Execute local-only sync: git init + add + commit, no fetch/pull/push."""

        path_str = config.options.get("path")
        if not isinstance(path_str, str) or not path_str.strip():
            return self._result(
                config,
                context,
                _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="missing_repo_and_path",
                    error="Hook config requires either 'remote' or 'path'.",
                ),
                start=start,
            )

        local_path = Path(path_str).expanduser().resolve()
        ok, error = self._ensure_local_repo(local_path)
        if not ok:
            logger.warning(
                "git_autosync_local_init_failed",
                path=str(local_path),
                error=error,
            )
            return self._result(
                config,
                context,
                _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="local_init_failed",
                    error=error,
                ),
                start=start,
                clone_path=str(local_path),
                context_name="local",
            )

        conflict_policy = config.options.get("conflict_policy", "abort")
        try:
            outcome = self._sync(
                str(local_path),
                config.exclude,
                conflict_policy,
                local_only=True,
                context=context,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(
                "git_autosync_runtime_error",
                clone_path=str(local_path),
                error=str(exc),
            )
            outcome = _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="git_runtime_error",
                error=str(exc),
            )
        return self._result(
            config,
            context,
            outcome,
            start=start,
            clone_path=str(local_path),
            context_name="local",
        )

    def _ensure_local_repo(self, path: Path) -> tuple[bool, str | None]:
        """Ensure path is a git repository, initializing one if needed."""

        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"Could not create directory {path}: {exc}"

        if (path / ".git").exists():
            return True, None

        result = self._run_git(str(path), ["init"], timeout=30)
        if result.returncode != 0:
            return False, f"git init failed: {result.stderr[:_MAX_ERROR_CHARS]}"
        return True, None

    def _resolve_remote_url(self, config: Hook) -> str | None:
        """Resolve remote URL from options first, then legacy top-level repo."""

        options_remote = config.options.get("remote")
        if isinstance(options_remote, str) and options_remote.strip():
            return options_remote

        if config.remote is not None and config.remote.strip():
            return config.remote
        return None

    def _ensure_clone(self, remote_url: str, clone_path: Path) -> tuple[bool, str | None]:
        """Ensure repository is cloned and remote matches."""

        if clone_path.exists():
            git_dir = clone_path / ".git"
            if not git_dir.exists():
                return False, f"Path exists but is not a git repository: {clone_path}"

            current_remote = self._get_remote_url(clone_path)
            if current_remote is None:
                return False, f"Could not read origin remote from: {clone_path}"

            if normalize_repo_url(current_remote) != normalize_repo_url(remote_url):
                return False, (
                    "Clone exists but remote differs. "
                    f"Expected: {remote_url}, Found: {current_remote}"
                )
            return True, None

        # Clone
        clone_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", remote_url, str(clone_path)],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode != 0:
            return False, f"git clone failed: {result.stderr[:_MAX_ERROR_CHARS]}"
        return True, None

    def _get_remote_url(self, clone_path: Path) -> str | None:
        """Get the origin remote URL from an existing clone."""

        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=clone_path,
            capture_output=True,
            text=True,
            timeout=_REMOTE_TIMEOUT_SECS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _is_rebase_in_progress(self, clone_path: str) -> bool:
        """Check if a rebase is in progress. Handles linked worktrees."""

        for rebase_dir in ("rebase-merge", "rebase-apply"):
            result = self._run_git(
                clone_path,
                ["rev-parse", "--git-path", rebase_dir],
                timeout=5,
            )
            if result.returncode != 0:
                continue

            path_str = result.stdout.strip()
            if not path_str:
                continue

            path = Path(path_str)
            if not path.is_absolute():
                path = Path(clone_path) / path

            if path.exists():
                return True

        return False

    def _is_merge_in_progress(self, clone_path: str) -> bool:
        """Check if a merge is in progress."""

        result = self._run_git(
            clone_path,
            ["rev-parse", "--git-path", "MERGE_HEAD"],
            timeout=5,
        )
        if result.returncode != 0:
            return False

        path_str = result.stdout.strip()
        if not path_str:
            return False

        path = Path(path_str)
        if not path.is_absolute():
            path = Path(clone_path) / path

        return path.exists()

    def _ensure_default_ignores(self, clone_path: str) -> None:
        """Ensure core autosync ignore patterns exist in .git/info/exclude."""

        required_patterns = AUTOSYNC_IGNORE_PATTERNS
        result = self._run_git(clone_path, ["rev-parse", "--git-path", "info/exclude"], timeout=5)
        if result.returncode != 0:
            logger.warning(
                "git_autosync_default_ignores_path_failed",
                clone_path=clone_path,
                error=self._format_git_error(
                    "git rev-parse --git-path info/exclude failed",
                    result,
                ),
            )
            return

        path_str = result.stdout.strip()
        if not path_str:
            return

        exclude_path = Path(path_str)
        if not exclude_path.is_absolute():
            exclude_path = Path(clone_path) / exclude_path

        try:
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        except OSError as exc:
            logger.warning(
                "git_autosync_default_ignores_read_failed",
                clone_path=clone_path,
                error=str(exc),
            )
            return

        existing_lines = {line.strip() for line in existing.splitlines() if line.strip()}
        missing_patterns = [
            pattern for pattern in required_patterns if pattern not in existing_lines
        ]
        if not missing_patterns:
            return

        new_content = existing
        if new_content and not new_content.endswith("\n"):
            new_content += "\n"
        new_content += "\n".join(missing_patterns) + "\n"

        tmp_path = exclude_path.with_suffix(exclude_path.suffix + ".tmp")
        try:
            tmp_path.write_text(new_content, encoding="utf-8")
            tmp_path.replace(exclude_path)
        except OSError as exc:
            logger.warning(
                "git_autosync_default_ignores_write_failed",
                clone_path=clone_path,
                error=str(exc),
            )
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def _get_current_branch(self, clone_path: str) -> str | None:
        result = self._run_git(clone_path, ["branch", "--show-current"], timeout=5)
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch or None

    def _stash_excluded_files(self, clone_path: str, excludes: tuple[str, ...]) -> bool:
        """Stash dirty excluded files before merge. Returns True if stash was created."""

        status = self._run_git(
            clone_path,
            ["status", "--porcelain", "-z"],
            timeout=_DIFF_TIMEOUT_SECS,
        )
        if status.returncode != 0 or not status.stdout:
            return False

        dirty_files: list[str] = []
        for entry in status.stdout.split("\0"):
            if len(entry) < 4:
                continue
            filepath = entry[3:]
            if self._is_excluded_path(filepath, excludes):
                dirty_files.append(filepath)

        if not dirty_files:
            return False

        result = self._run_git(
            clone_path,
            [
                "stash",
                "push",
                "--include-untracked",
                "-m",
                "autosync-exclude",
                "--",
                *dirty_files,
            ],
            timeout=_STASH_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            logger.warning(
                "git_autosync_stash_push_failed",
                clone_path=clone_path,
                error=self._format_git_error("stash push failed", result),
            )
            return False

        return True

    def _unstash_excluded_files(self, clone_path: str) -> None:
        """Pop the autosync-exclude stash if it exists."""

        result = self._run_git(clone_path, ["stash", "list", "--max-count=1"], timeout=5)
        if result.returncode != 0:
            return

        if "autosync-exclude" not in result.stdout:
            return

        pop = self._run_git(clone_path, ["stash", "pop"], timeout=_STASH_TIMEOUT_SECS)
        if pop.returncode != 0:
            logger.warning(
                "git_autosync_stash_pop_failed",
                clone_path=clone_path,
                error=self._format_git_error("stash pop failed", pop),
            )

    def _get_conflict_paths(self, clone_path: str) -> list[str]:
        """Get paths that have merge conflicts from git status."""

        result = self._run_git(
            clone_path,
            ["diff", "--name-only", "--diff-filter=U"],
            timeout=_DIFF_TIMEOUT_SECS,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [path.strip() for path in result.stdout.splitlines() if path.strip()]

        status = self._run_git(
            clone_path,
            ["status", "--porcelain", "-z"],
            timeout=_DIFF_TIMEOUT_SECS,
        )
        if status.returncode != 0 or not status.stdout:
            return []

        unmerged_states = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}
        paths: list[str] = []
        for entry in status.stdout.split("\0"):
            if len(entry) < 4:
                continue
            if entry[:2] in unmerged_states:
                paths.append(entry[3:])
        return paths

    def _get_sha(self, clone_path: str, ref: str) -> str:
        """Get the SHA for a ref."""

        result = self._run_git(clone_path, ["rev-parse", ref], timeout=5)
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip()

    def _detect_conflict_type(self, clone_path: str, paths: list[str], remote_branch: str) -> str:
        """Best-effort conflict type detection."""

        for path in paths:
            local_exists = (
                self._run_git(clone_path, ["cat-file", "-t", f"HEAD:{path}"], timeout=5).returncode
                == 0
            )
            remote_exists = (
                self._run_git(
                    clone_path,
                    ["cat-file", "-t", f"origin/{remote_branch}:{path}"],
                    timeout=5,
                ).returncode
                == 0
            )

            if local_exists and not remote_exists:
                return "delete/modify"
            if not local_exists and remote_exists:
                return "delete/modify"

        return "content"

    def _get_commit_stats(self, clone_path: str) -> dict[str, int]:
        """Get file change stats from the latest commit."""

        result = self._run_git(
            clone_path,
            ["diff", "--numstat", "HEAD~1..HEAD"],
            timeout=_DIFF_TIMEOUT_SECS,
        )
        stats = {"added": 0, "modified": 0, "deleted": 0, "renamed": 0}
        if result.returncode != 0:
            return stats

        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue

            added, deleted, path = parts[0], parts[1], parts[2]
            if "=>" in path:
                stats["renamed"] += 1
                continue

            if added == "-" or deleted == "-":
                stats["modified"] += 1
            elif added != "0" and deleted == "0":
                stats["added"] += 1
            elif added == "0" and deleted != "0":
                stats["deleted"] += 1
            else:
                stats["modified"] += 1

        return stats

    def _sync(
        self,
        clone_path: str,
        excludes: tuple[str, ...],
        conflict_policy: object = "abort",
        local_only: bool = False,
        context: HookContext | None = None,
    ) -> _SyncOutcome:
        """Execute commit-first merge-based sync workflow."""

        self._ensure_default_ignores(clone_path)

        if self._is_merge_in_progress(clone_path):
            if conflict_policy == "abort":
                abort = self._run_git(
                    clone_path,
                    ["merge", "--abort"],
                    timeout=_MERGE_ABORT_TIMEOUT_SECS,
                )
                if abort.returncode != 0:
                    message = self._format_git_error("git merge --abort failed", abort)
                    return _SyncOutcome(
                        outcome="skipped",
                        success=True,
                        skipped=True,
                        skip_reason="merge_abort_failed",
                        error=message,
                    )
            else:
                return _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="existing_merge_conflict",
                    error=f"Merge already in progress at {clone_path}.",
                )

        if self._is_rebase_in_progress(clone_path):
            if conflict_policy == "abort":
                abort = self._run_git(
                    clone_path,
                    ["rebase", "--abort"],
                    timeout=_MERGE_ABORT_TIMEOUT_SECS,
                )
                if abort.returncode != 0:
                    message = self._format_git_error("git rebase --abort failed", abort)
                    return _SyncOutcome(
                        outcome="skipped",
                        success=True,
                        skipped=True,
                        skip_reason="rebase_abort_failed",
                        error=message,
                    )
            else:
                return _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="existing_rebase_conflict",
                    error=(
                        f"Rebase already in progress at {clone_path}. "
                        "Resolve conflicts before next sync."
                    ),
                )

        # 3. Stage everything
        add = self._run_git(clone_path, ["add", "-A"], timeout=_ADD_TIMEOUT_SECS)
        if add.returncode != 0:
            message = self._format_git_error("git add failed", add)
            logger.warning("git_autosync_add_failed", clone_path=clone_path, error=message)
            return _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="add_failed",
                error=message,
            )

        # 4. Reset excluded staged paths
        if excludes:
            excluded_paths_result = self._run_git(
                clone_path,
                ["diff", "--cached", "--name-only", "-z"],
                timeout=_DIFF_TIMEOUT_SECS,
            )
            if excluded_paths_result.returncode != 0:
                message = self._format_git_error(
                    "git diff --cached --name-only failed",
                    excluded_paths_result,
                )
                logger.warning(
                    "git_autosync_exclude_scan_failed",
                    clone_path=clone_path,
                    error=message,
                )
                return _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="exclude_scan_failed",
                    error=message,
                )

            staged_paths = self._parse_nul_paths(excluded_paths_result.stdout)
            excluded_paths = [
                path for path in staged_paths if self._is_excluded_path(path, excludes)
            ]
            if excluded_paths:
                reset = self._run_git(
                    clone_path,
                    ["reset", "--quiet", "--", *excluded_paths],
                    timeout=_ADD_TIMEOUT_SECS,
                )
                if reset.returncode != 0:
                    message = self._format_git_error("git reset excluded paths failed", reset)
                    logger.warning(
                        "git_autosync_exclude_reset_failed",
                        clone_path=clone_path,
                        error=message,
                    )
                    return _SyncOutcome(
                        outcome="skipped",
                        success=True,
                        skipped=True,
                        skip_reason="exclude_reset_failed",
                        error=message,
                    )

        # 5. Stash dirty excluded files
        stashed_excludes = False
        if excludes:
            stashed_excludes = self._stash_excluded_files(clone_path, excludes)

        just_committed = False
        just_merged = False
        files_changed: dict[str, int] | None = None

        # 6. Check if anything to commit
        staged_check = self._run_git(
            clone_path,
            ["diff", "--cached", "--quiet"],
            timeout=_DIFF_TIMEOUT_SECS,
        )
        if staged_check.returncode not in (0, 1):
            if stashed_excludes:
                self._unstash_excluded_files(clone_path)
            message = self._format_git_error("git diff --cached --quiet failed", staged_check)
            logger.warning("git_autosync_staged_check_failed", clone_path=clone_path, error=message)
            return _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="staged_check_failed",
                error=message,
            )

        if staged_check.returncode == 1:
            commit_message = f"autosync: {datetime.now(UTC).isoformat()}"
            commit = self._run_git(
                clone_path,
                ["commit", "-m", commit_message],
                timeout=_COMMIT_TIMEOUT_SECS,
            )
            if commit.returncode != 0:
                if not self._looks_like_nothing_to_commit(commit):
                    if stashed_excludes:
                        self._unstash_excluded_files(clone_path)
                    message = self._format_git_error("git commit failed", commit)
                    logger.warning(
                        "git_autosync_commit_failed",
                        clone_path=clone_path,
                        error=message,
                    )
                    return _SyncOutcome(
                        outcome="skipped",
                        success=True,
                        skipped=True,
                        skip_reason="commit_failed",
                        error=message,
                    )
            else:
                just_committed = True
                files_changed = self._get_commit_stats(clone_path)

        # 7. Local-only mode ends after local commit handling
        if local_only:
            if stashed_excludes:
                self._unstash_excluded_files(clone_path)
            if just_committed:
                write_sync_state(Path(clone_path), outcome="success")
                return _SyncOutcome(outcome="success", success=True, files_changed=files_changed)
            write_sync_state(Path(clone_path), outcome="nothing_to_sync")
            return _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="nothing_to_sync",
            )

        # 8. Fetch origin
        fetch = self._run_git(clone_path, ["fetch", "origin"], timeout=_FETCH_TIMEOUT_SECS)
        if fetch.returncode != 0:
            if stashed_excludes:
                self._unstash_excluded_files(clone_path)
            message = self._format_git_error("git fetch origin failed", fetch)
            logger.warning("git_autosync_fetch_failed", clone_path=clone_path, error=message)
            return _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="fetch_failed",
                error=message,
            )

        # 9. Divergence check
        branch = self._get_current_branch(clone_path)
        divergence = self._check_divergence(clone_path, branch)
        if divergence is None:
            if stashed_excludes:
                self._unstash_excluded_files(clone_path)
            return _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="divergence_check_failed",
                error="Could not determine ahead/behind status.",
            )
        ahead, behind = divergence

        # 10. Merge when behind
        if behind > 0:
            if not branch:
                if stashed_excludes:
                    self._unstash_excluded_files(clone_path)
                return _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="merge_branch_unknown",
                    error="Could not resolve current branch for merge.",
                )

            merge = self._run_git(
                clone_path,
                ["merge", f"origin/{branch}"],
                timeout=_MERGE_TIMEOUT_SECS,
            )
            if merge.returncode != 0:
                message = self._format_git_error(f"git merge origin/{branch} failed", merge)

                # Merge conflict is detected by active MERGE_HEAD.
                if self._is_merge_in_progress(clone_path):
                    conflict_paths = self._get_conflict_paths(clone_path)
                    remote_ref = f"origin/{branch}"
                    local_sha = self._get_sha(clone_path, "HEAD")
                    remote_sha = self._get_sha(clone_path, remote_ref)
                    conflict_type = self._detect_conflict_type(clone_path, conflict_paths, branch)

                    abort = self._run_git(
                        clone_path,
                        ["merge", "--abort"],
                        timeout=_MERGE_ABORT_TIMEOUT_SECS,
                    )
                    if abort.returncode != 0:
                        if stashed_excludes:
                            self._unstash_excluded_files(clone_path)
                        abort_error = self._format_git_error("git merge --abort failed", abort)
                        logger.error(
                            "git_autosync_merge_abort_failed",
                            clone_path=clone_path,
                            merge_error=message,
                            abort_error=abort_error,
                        )
                        return _SyncOutcome(
                            outcome="skipped",
                            success=True,
                            skipped=True,
                            skip_reason="merge_abort_failed",
                            error=f"{message}; {abort_error}",
                        )

                    if has_unresolved_conflict(Path(clone_path)):
                        if stashed_excludes:
                            self._unstash_excluded_files(clone_path)
                        logger.warning(
                            "git_autosync_merge_conflict_already_recorded",
                            clone_path=clone_path,
                        )
                        write_sync_state(Path(clone_path), outcome="conflict_detected")
                        return _SyncOutcome(
                            outcome="skipped",
                            success=True,
                            skipped=True,
                            skip_reason="conflict_detected",
                            error="Unresolved autosync conflict already recorded.",
                        )

                    conflict_id = generate_conflict_id()
                    context_name = Path(clone_path).name
                    event_name = context.event_name if context is not None else "spawn.finalized"
                    spawn_id = context.spawn_id if context is not None else None

                    record = ConflictRecord(
                        id=conflict_id,
                        context=context_name,
                        sync_root=clone_path,
                        conflict_type=conflict_type,
                        paths=tuple(conflict_paths),
                        local_sha=local_sha,
                        remote_sha=remote_sha,
                        remote_branch=branch,
                        event_name=event_name,
                        spawn_id=spawn_id,
                        created_at=datetime.now(UTC).isoformat(),
                        resolved=False,
                    )
                    write_conflict(Path(clone_path), record)

                    notice_written = append_conflict_notice(
                        Path(clone_path),
                        conflict_id,
                        conflict_paths,
                        branch,
                    )
                    if notice_written:
                        add_notice = self._run_git(
                            clone_path,
                            ["add", "AGENTS.md"],
                            timeout=_ADD_TIMEOUT_SECS,
                        )
                        if add_notice.returncode == 0:
                            notice_commit = self._run_git(
                                clone_path,
                                ["commit", "-m", f"autosync: conflict notice {conflict_id}"],
                                timeout=_COMMIT_TIMEOUT_SECS,
                            )
                            if notice_commit.returncode == 0:
                                files_changed = self._get_commit_stats(clone_path)
                            elif not self._looks_like_nothing_to_commit(notice_commit):
                                logger.warning(
                                    "git_autosync_notice_commit_failed",
                                    clone_path=clone_path,
                                    error=self._format_git_error(
                                        "git commit conflict notice failed",
                                        notice_commit,
                                    ),
                                )

                    write_sync_state(
                        Path(clone_path),
                        outcome="conflict_detected",
                        conflict_id=conflict_id,
                    )
                    if stashed_excludes:
                        self._unstash_excluded_files(clone_path)
                    return _SyncOutcome(
                        outcome="skipped",
                        success=True,
                        skipped=True,
                        skip_reason="conflict_detected",
                        error=message,
                        conflict_id=conflict_id,
                        conflict_paths=tuple(conflict_paths),
                        files_changed=files_changed,
                    )

                if stashed_excludes:
                    self._unstash_excluded_files(clone_path)
                logger.warning("git_autosync_merge_failed", clone_path=clone_path, error=message)
                return _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="merge_failed",
                    error=message,
                )

            just_merged = True
            files_changed = self._get_commit_stats(clone_path)
            divergence_after_merge = self._check_divergence(clone_path, branch)
            if divergence_after_merge is not None:
                ahead, _ = divergence_after_merge

        # 11. Push when local commits are present.
        if ahead > 0 or just_committed or just_merged:
            push = self._run_git(clone_path, ["push"], timeout=_PUSH_TIMEOUT_SECS)
            if push.returncode != 0:
                if stashed_excludes:
                    self._unstash_excluded_files(clone_path)
                message = self._format_git_error("git push failed", push)
                logger.warning("git_autosync_push_failed", clone_path=clone_path, error=message)
                return _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="push_failed",
                    error=message,
                    files_changed=files_changed,
                )

            write_sync_state(Path(clone_path), outcome="success")
            if stashed_excludes:
                self._unstash_excluded_files(clone_path)
            return _SyncOutcome(
                outcome="success",
                success=True,
                files_changed=files_changed,
            )

        # 12. Nothing to sync.
        write_sync_state(Path(clone_path), outcome="nothing_to_sync")
        if stashed_excludes:
            self._unstash_excluded_files(clone_path)
        return _SyncOutcome(
            outcome="skipped",
            success=True,
            skipped=True,
            skip_reason="nothing_to_sync",
        )

    def _check_divergence(
        self,
        clone_path: str,
        branch: str | None = None,
    ) -> tuple[int, int] | None:
        """Check commits ahead/behind upstream. Returns (ahead, behind).

        When rev-list fails, logs a warning and attempts fallback. Returns None
        when divergence cannot be determined.
        """

        result = self._run_git(
            clone_path,
            ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            timeout=_DIFF_TIMEOUT_SECS,
        )
        if result.returncode == 0:
            try:
                parts = result.stdout.strip().split()
                return (int(parts[0]), int(parts[1]))
            except (IndexError, ValueError):
                pass

        error_msg = (result.stderr or result.stdout or "").strip()
        logger.warning(
            "git_autosync_divergence_check_failed",
            clone_path=clone_path,
            error=error_msg,
        )

        if branch:
            remote_ref = f"origin/{branch}"
            fallback = self._run_git(
                clone_path,
                ["log", "--oneline", f"HEAD..{remote_ref}"],
                timeout=_DIFF_TIMEOUT_SECS,
            )
            if fallback.returncode == 0:
                behind = len([line for line in fallback.stdout.splitlines() if line.strip()])
                ahead_result = self._run_git(
                    clone_path,
                    ["log", "--oneline", f"{remote_ref}..HEAD"],
                    timeout=_DIFF_TIMEOUT_SECS,
                )
                if ahead_result.returncode == 0:
                    ahead = len([line for line in ahead_result.stdout.splitlines() if line.strip()])
                    return (ahead, behind)

                logger.warning(
                    "git_autosync_divergence_fallback_failed",
                    clone_path=clone_path,
                    error=(ahead_result.stderr or ahead_result.stdout or "").strip(),
                )

        return None

    def _run_git(
        self,
        work_dir: str,
        args: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},  # Locale-independent
        )

    def _result(
        self,
        config: Hook,
        context: HookContext,
        outcome: _SyncOutcome,
        *,
        start: float,
        clone_path: str | None = None,
        context_name: str | None = None,
    ) -> HookResult:
        duration = int((time.monotonic() - start) * 1000)

        log_kwargs: dict[str, Any] = {
            "hook_event": context.event_name,
            "outcome": outcome.outcome,
            "duration_ms": duration,
        }
        if clone_path:
            log_kwargs["clone_path"] = clone_path
        if context_name:
            log_kwargs["context_name"] = context_name
        if outcome.skip_reason:
            log_kwargs["skip_reason"] = outcome.skip_reason
        if outcome.conflict_id:
            log_kwargs["conflict_id"] = outcome.conflict_id
        if outcome.conflict_paths:
            log_kwargs["conflict_paths"] = list(outcome.conflict_paths)
        if outcome.files_changed:
            log_kwargs["files_changed"] = outcome.files_changed

        logger.info("git_autosync_completed", **log_kwargs)

        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome=outcome.outcome,
            success=outcome.success,
            skipped=outcome.skipped,
            skip_reason=outcome.skip_reason,
            error=outcome.error,
            duration_ms=duration,
        )

    def _format_git_error(
        self,
        prefix: str,
        result: subprocess.CompletedProcess[str],
    ) -> str:
        details = (result.stderr or result.stdout or "").strip()
        if details:
            return f"{prefix}: {details[:_MAX_ERROR_CHARS]}"
        return f"{prefix}: exit {result.returncode}"

    def _looks_like_nothing_to_commit(self, result: subprocess.CompletedProcess[str]) -> bool:
        haystack = f"{result.stdout}\n{result.stderr}".lower()
        return "nothing to commit" in haystack or "no changes added to commit" in haystack

    def _parse_nul_paths(self, raw: str) -> tuple[str, ...]:
        if not raw:
            return ()
        return tuple(path for path in raw.split("\0") if path)

    def _is_excluded_path(self, path: str, excludes: tuple[str, ...]) -> bool:
        posix_path = path.replace("\\", "/")
        basename = PurePosixPath(posix_path).name
        for pattern in excludes:
            normalized_pattern = pattern.replace("\\", "/").strip()
            if not normalized_pattern:
                continue

            if normalized_pattern.endswith("/"):
                prefix = normalized_pattern.rstrip("/")
                if posix_path == prefix or posix_path.startswith(f"{prefix}/"):
                    return True
                continue

            if fnmatch.fnmatch(posix_path, normalized_pattern):
                return True
            if "/" not in normalized_pattern and fnmatch.fnmatch(basename, normalized_pattern):
                return True
        return False


GIT_AUTOSYNC = GitAutosync()

__all__ = ["GIT_AUTOSYNC", "GitAutosync"]
