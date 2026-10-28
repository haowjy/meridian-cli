"""Authoritative runtime-context resolution from ``MERIDIAN_*`` inputs.

This module defines the canonical environment-to-context translation used by
launch, ops, and child-environment composition paths.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import context_env_key, resolve_context_paths
from meridian.lib.core.depth import (
    DEPTH_ENV,
    child_meridian_depth,
    parse_meridian_depth,
)
from meridian.lib.core.types import SpawnId
from meridian.lib.state import paths as state_paths
from meridian.lib.state import session_store, spawn_store, work_store
from meridian.lib.state.work_scope import WorkScope, resolve_work_scope_from_parts
from meridian.lib.state.work_state import warn_missing_work_item


class ContextBackend(Protocol):
    """Backend interface for the authoritative context resolution workflow."""

    def get_session_active_work_id(self, runtime_root: Path, chat_id: str) -> str | None:
        """Look up the active work ID for a session."""
        ...

    def get_execution_chat(self, runtime_root: Path, spawn_id: SpawnId) -> str | None:
        """The conversation an execution acts on, from its own spawn row."""
        ...


class LocalFilesystemBackend:
    """Default context backend backed by local state modules."""

    def get_session_active_work_id(self, runtime_root: Path, chat_id: str) -> str | None:
        # The same row read every work command uses; a projection would be a second
        # answer to "what is this chat's item" that could disagree with the writes.
        return session_store.get_session_active_work_id(runtime_root, chat_id)

    def get_execution_chat(self, runtime_root: Path, spawn_id: SpawnId) -> str | None:
        row = spawn_store.get_spawn(runtime_root, spawn_id)
        return ((row.continue_chat_id or "") if row is not None else "").strip() or None


def _execution_chat(
    env: Mapping[str, str],
    runtime_root: Path,
    backend: ContextBackend | None = None,
) -> str | None:
    spawn_id = env.get("MERIDIAN_SPAWN_ID", "").strip()
    if not spawn_id:
        return None
    return (backend or LocalFilesystemBackend()).get_execution_chat(
        runtime_root, SpawnId(spawn_id)
    )


@dataclass(frozen=True)
class ResolvedContext:
    """Canonical immutable runtime context resolved from ``MERIDIAN_*`` inputs."""

    spawn_id: SpawnId | None = None
    parent_spawn_id: SpawnId | None = None
    depth: int = 0
    project_root: Path | None = None
    runtime_root: Path | None = None
    chat_id: str = ""
    work_id: str | None = None
    work_dir: Path | None = None
    work_scope: WorkScope | None = None
    kb_dir: Path | None = None
    context_dirs: tuple[tuple[str, Path], ...] = ()

    @classmethod
    def from_environment(
        cls,
        *,
        explicit_work_id: str | None = None,
        explicit_chat_id: str | None = None,
        explicit_project_root: Path | None = None,
        explicit_runtime_root: Path | None = None,
        backend: ContextBackend | None = None,
        context_config: ContextConfig | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Resolve and freeze canonical runtime context from ``MERIDIAN_*`` values.

        *environ* is the environment of the execution being described; a process
        describing another one (the idle service, a notification label) passes that
        execution's own, and this process's is the default.

        When *explicit_project_root* or *explicit_runtime_root* are supplied they
        take precedence over ``MERIDIAN_PROJECT_DIR``. ``runtime_root`` is
        *explicit_runtime_root*, else the ``_MERIDIAN_RUNTIME_DIR`` override (the
        same rule as ``ops/runtime.py``), else derived from ``project_root`` identity
        into user state.
        """

        import os

        from meridian.lib.state.runtime_root import (
            derive_runtime_root_from_project,
            runtime_override_root,
        )

        env = os.environ if environ is None else environ
        backend_impl = backend or LocalFilesystemBackend()

        spawn_id_raw = env.get("MERIDIAN_SPAWN_ID", "").strip()
        spawn_id = SpawnId(spawn_id_raw) if spawn_id_raw else None
        parent_spawn_id_raw = env.get("_MERIDIAN_PARENT_SPAWN_ID", "").strip()
        depth_raw = env.get(DEPTH_ENV, "0").strip()
        project_root_raw = env.get("MERIDIAN_PROJECT_DIR", "").strip()
        explicit_chat_id_raw = (explicit_chat_id or "").strip()
        explicit_work_id_raw = (explicit_work_id or "").strip()
        if explicit_chat_id_raw:
            chat_id_raw = explicit_chat_id_raw
            work_id_raw = ""
            work_dir_raw = ""
        else:
            chat_id_raw = env.get("MERIDIAN_CHAT_ID", "").strip()
            work_id_raw = env.get("MERIDIAN_ACTIVE_WORK_ID", "").strip()
            work_dir_raw = env.get("MERIDIAN_ACTIVE_WORK_DIR", "").strip()

        depth = parse_meridian_depth(depth_raw)

        project_root = (
            explicit_project_root
            if explicit_project_root is not None
            else Path(project_root_raw)
            if project_root_raw
            else None
        )
        if explicit_runtime_root is not None:
            runtime_root = explicit_runtime_root
        elif (override_root := runtime_override_root(project_root, env)) is not None:
            runtime_root = override_root
        elif project_root is not None:
            runtime_root = derive_runtime_root_from_project(project_root)
        else:
            runtime_root = None

        # MERIDIAN_CHAT_ID and MERIDIAN_ACTIVE_WORK_ID are a launch snapshot naming the
        # conversation that launched this run (a primary's own chat; a spawn's
        # launcher's chat): only the execution's own session row names the
        # conversation it acts on. A primary's row follows a chat rotation
        # (Claude /clear, /resume); a spawn's row is its own chat even though the env
        # still names the launcher's. With no row — an unmanaged
        # shell — the env is all there is. Explicit arguments keep their precedence.
        if not explicit_chat_id_raw and runtime_root is not None:
            execution_chat = _execution_chat(env, runtime_root, backend_impl)
            if execution_chat is not None:
                chat_id_raw = execution_chat
                work_id_raw = ""
                work_dir_raw = ""

        # Authoritative work-ID precedence: explicit override > the execution's own
        # chat row > MERIDIAN_ACTIVE_WORK_ID (only outside a managed run).
        work_id: str | None = None
        work_id_source: Literal["explicit", "env", "session", "none"] = "none"
        if explicit_work_id_raw:
            work_id = explicit_work_id_raw
            work_id_source = "explicit"
        elif work_id_raw:
            work_id = work_id_raw
            work_id_source = "env"
        elif runtime_root is not None and chat_id_raw:
            session_work_id = backend_impl.get_session_active_work_id(runtime_root, chat_id_raw)
            if session_work_id:
                work_id = session_work_id
                work_id_source = "session"

        project_paths = (
            state_paths.resolve_project_paths_from_context(
                project_root,
                context_config=context_config,
            )
            if project_root is not None
            else None
        )

        # A row or env can name an item that no longer exists: a rename or delete does
        # not rewrite chats that ended, and a launch snapshot outlives its item. That
        # name is no item. Only an explicit name creates one, so a stale one never
        # brings a deleted or renamed item back through `--from` or an ambient launch.
        if (
            work_id
            and work_id_source != "explicit"
            and project_paths is not None
            and project_paths.work_dir is not None
            and not work_store.work_item_exists(project_paths.root_dir, work_id)
        ):
            warn_missing_work_item(work_id, chat_id=chat_id_raw)
            work_id = None
            work_dir_raw = ""

        bound_work_dir: Path | None = None

        if work_id and project_paths is not None and project_paths.work_dir is not None:
            # The item's directory where it actually is: `work done` moves it to the
            # archive, and a command that resolved the other location would name a
            # path that no longer exists.
            bound_work_dir = work_store.work_scratch_dir(project_paths.root_dir, work_id)
        elif (
            work_dir_raw
            and not explicit_work_id_raw
            and work_id_source in ("none", "env")
        ):
            # Honor launch-bound dirs for ambient scopes and env-carried work IDs.
            # Session-switched work IDs recompute canonical roots, so stale ambient
            # MERIDIAN_ACTIVE_WORK_DIR from process launch must not shadow them.
            bound_work_dir = Path(work_dir_raw).expanduser()

        work_scope = resolve_work_scope_from_parts(
            project_root=project_root,
            spawn_id=spawn_id,
            work_id=work_id,
            bound_work_dir=bound_work_dir,
            runtime_root=runtime_root,
        )
        work_dir = work_scope.root if work_scope is not None else None

        kb_dir = project_paths.kb_dir if project_paths is not None else None

        resolved_config = context_config
        if resolved_config is None and project_root is not None:
            resolved_config = state_paths.load_context_config(project_root)

        context_dirs: tuple[tuple[str, Path], ...] = ()
        if project_root is not None:
            effective_config = resolved_config or ContextConfig()
            resolved_context_paths = resolve_context_paths(project_root, effective_config)
            builtins = (
                ("work", resolved_context_paths.work_root),
                ("work_archive", resolved_context_paths.work_archive),
                ("kb", resolved_context_paths.kb_root),
            )
            resolved_builtins = tuple(
                (name, path) for name, path in builtins if path is not None
            )
            context_dirs = resolved_builtins + tuple(
                sorted((name, path) for name, (path, _) in resolved_context_paths.extra.items())
            )

        return cls(
            spawn_id=spawn_id,
            parent_spawn_id=SpawnId(parent_spawn_id_raw) if parent_spawn_id_raw else None,
            depth=depth,
            project_root=project_root,
            runtime_root=runtime_root,
            chat_id=chat_id_raw,
            work_id=work_id,
            work_dir=work_dir,
            work_scope=work_scope,
            kb_dir=kb_dir,
            context_dirs=context_dirs,
        )

    def child_env_overrides(
        self, *, increment_depth: bool = True, child_spawn_id: str | None = None
    ) -> dict[str, str]:
        """Produce `MERIDIAN_*` env overrides for child processes."""

        next_depth = child_meridian_depth(self.depth, increment_depth=increment_depth)
        overrides: dict[str, str] = {"_MERIDIAN_DEPTH": str(next_depth)}
        if child_spawn_id is not None:
            overrides["MERIDIAN_SPAWN_ID"] = child_spawn_id
        elif self.spawn_id is not None:
            overrides["MERIDIAN_SPAWN_ID"] = str(self.spawn_id)
        if self.spawn_id is not None:
            overrides["_MERIDIAN_PARENT_SPAWN_ID"] = str(self.spawn_id)
        if self.project_root is not None:
            overrides["MERIDIAN_PROJECT_DIR"] = self.project_root.as_posix()
        if self.chat_id:
            overrides["MERIDIAN_CHAT_ID"] = self.chat_id
        if self.work_id:
            overrides["MERIDIAN_ACTIVE_WORK_ID"] = self.work_id
        if self.work_dir is not None:
            overrides["MERIDIAN_ACTIVE_WORK_DIR"] = self.work_dir.as_posix()
        for context_name, context_dir in self.context_dirs:
            if not context_name.strip():
                continue
            env_key = context_env_key(context_name)
            overrides[env_key] = context_dir.as_posix()
        return overrides
