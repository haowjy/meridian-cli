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

import structlog

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
_PULL_TIMEOUT_SECS = 60
_ADD_TIMEOUT_SECS = 30
_COMMIT_TIMEOUT_SECS = 30
_PUSH_TIMEOUT_SECS = 60
_REBASE_ABORT_TIMEOUT_SECS = 30
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
            )

        try:
            conflict_policy = config.options.get("conflict_policy", "leave")
            outcome = self._sync(str(clone_path), config.exclude, conflict_policy)
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
        return self._result(config, context, outcome, start=start)

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
            )

        conflict_policy = config.options.get("conflict_policy", "leave")
        try:
            outcome = self._sync(str(local_path), config.exclude, conflict_policy, local_only=True)
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
        return self._result(config, context, outcome, start=start)

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

    def _sync(
        self,
        clone_path: str,
        excludes: tuple[str, ...],
        conflict_policy: object = "leave",
        local_only: bool = False,
    ) -> _SyncOutcome:
        """Execute commit-first sync workflow.

        Order: add -A -> commit (if needed) -> fetch -> pull --rebase (if behind) -> push
        Committing first ensures local changes are safe before rebasing.

        When local_only=True, steps 5-8 (fetch/pull/push) are skipped — used for
        local-only repos that have no remote.
        """

        if self._is_rebase_in_progress(clone_path):
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

        # 1. Stage everything
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

        # 2. Apply excludes
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

        # 3. Check if anything to commit
        staged_check = self._run_git(
            clone_path,
            ["diff", "--cached", "--quiet"],
            timeout=_DIFF_TIMEOUT_SECS,
        )
        just_committed = False
        if staged_check.returncode not in (0, 1):
            message = self._format_git_error("git diff --cached --quiet failed", staged_check)
            logger.warning("git_autosync_staged_check_failed", clone_path=clone_path, error=message)
            return _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="staged_check_failed",
                error=message,
            )

        # 4. COMMIT FIRST when there are staged changes (before pull/rebase)
        if staged_check.returncode == 1:
            commit_message = f"autosync: {datetime.now(UTC).isoformat()}"
            commit = self._run_git(
                clone_path,
                ["commit", "-m", commit_message],
                timeout=_COMMIT_TIMEOUT_SECS,
            )
            if commit.returncode != 0:
                if not self._looks_like_nothing_to_commit(commit):
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

        # For local-only repos, stop here — no remote to fetch/pull/push.
        if local_only:
            if just_committed:
                return _SyncOutcome(outcome="success", success=True)
            return _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="nothing_to_sync",
            )

        # 5. Fetch upstream before checking divergence.
        fetch = self._run_git(clone_path, ["fetch", "origin"], timeout=_REMOTE_TIMEOUT_SECS)
        if fetch.returncode != 0:
            message = self._format_git_error("git fetch origin failed", fetch)
            logger.warning("git_autosync_fetch_failed", clone_path=clone_path, error=message)
            return _SyncOutcome(
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="fetch_failed",
                error=message,
            )

        # 6. Check ahead/behind against upstream.
        ahead, behind = self._check_divergence(clone_path)

        # 7. Pull when behind.
        if behind > 0:
            pull = self._run_git(
                clone_path,
                ["pull", "--rebase"],  # No --autostash
                timeout=_PULL_TIMEOUT_SECS,
            )
            if pull.returncode != 0:
                message = self._format_git_error("git pull --rebase failed", pull)

                # Detect rebase conflict via repo state (locale-independent)
                rebase_detected = self._is_rebase_in_progress(clone_path)

                if rebase_detected:
                    if conflict_policy == "abort":
                        abort = self._run_git(
                            clone_path,
                            ["rebase", "--abort"],
                            timeout=_REBASE_ABORT_TIMEOUT_SECS,
                        )
                        if abort.returncode != 0:
                            abort_error = (abort.stderr or "")[:500]
                            logger.error(
                                "git_autosync_rebase_abort_failed",
                                clone_path=clone_path,
                                pull_error=message,
                                abort_error=abort_error,
                            )
                            return _SyncOutcome(
                                outcome="skipped",
                                success=True,
                                skipped=True,
                                skip_reason="rebase_abort_failed",
                                error=f"{message}; abort failed: {(abort.stderr or '')[:200]}",
                            )

                        logger.warning(
                            "git_autosync_rebase_conflict_aborted",
                            clone_path=clone_path,
                            pull_error=message,
                        )
                        return _SyncOutcome(
                            outcome="skipped",
                            success=True,
                            skipped=True,
                            skip_reason="rebase_conflict",
                            error=message,
                        )

                    logger.warning(
                        "git_autosync_rebase_conflict_left",
                        clone_path=clone_path,
                        pull_error=message,
                    )
                    return _SyncOutcome(
                        outcome="skipped",
                        success=True,
                        skipped=True,
                        skip_reason="rebase_conflict",
                        error=(
                            f"Rebase conflict at {clone_path}. Conflicts left for review. {message}"
                        ),
                    )

                logger.warning("git_autosync_pull_failed", clone_path=clone_path, error=message)
                return _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="pull_failed",
                    error=message,
                )

            # Re-check after pull/rebase before deciding push.
            ahead, _ = self._check_divergence(clone_path)

        # 8. Push local commits when present.
        if ahead > 0 or just_committed:
            push = self._run_git(clone_path, ["push"], timeout=_PUSH_TIMEOUT_SECS)
            if push.returncode != 0:
                message = self._format_git_error("git push failed", push)
                logger.warning("git_autosync_push_failed", clone_path=clone_path, error=message)
                return _SyncOutcome(
                    outcome="skipped",
                    success=True,
                    skipped=True,
                    skip_reason="push_failed",
                    error=message,
                )
            return _SyncOutcome(outcome="success", success=True)

        # Nothing committed and no upstream divergence.
        return _SyncOutcome(
            outcome="skipped",
            success=True,
            skipped=True,
            skip_reason="nothing_to_sync",
        )

    def _check_divergence(self, clone_path: str) -> tuple[int, int]:
        """Check commits ahead/behind upstream. Returns (ahead, behind)."""

        result = self._run_git(
            clone_path,
            ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            timeout=_DIFF_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            return (0, 0)
        try:
            parts = result.stdout.strip().split()
            return (int(parts[0]), int(parts[1]))
        except (IndexError, ValueError):
            return (0, 0)

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
    ) -> HookResult:
        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome=outcome.outcome,
            success=outcome.success,
            skipped=outcome.skipped,
            skip_reason=outcome.skip_reason,
            error=outcome.error,
            duration_ms=int((time.monotonic() - start) * 1000),
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
