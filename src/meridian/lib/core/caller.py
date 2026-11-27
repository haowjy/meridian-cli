"""The calling session's identity: a spawn ID and the runtime root that ID belongs to.

A bare ID means nothing without its root. A claimed ID reaches any project, so a
command that acts on a session in another root must not record the caller's ID there:
a legacy ``p41`` in project A is a different session from ``p41`` in project B. Code that
keys state by "the caller" asks ``caller_in(root)`` and gets nothing for any other root.

Reading ``MERIDIAN_SPAWN_ID`` from ``os.environ`` outside this module is a lint error
(``meridian.dev.lints``), but only the literal forms: ``os.environ.get("MERIDIAN_SPAWN_ID")``,
``os.environ[...]`` and ``os.getenv(...)``. ``ResolvedContext`` and the notify and depth code
read it from an ``env`` mapping they were handed, always together with a root from the same
environment, and none writes into another root. ``RuntimeContext.caller_in`` is this module's
``CallerIdentity.in_root`` over the context's own (ID, root).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.core.types import SpawnId


@dataclass(frozen=True)
class CallerIdentity:
    runtime_root: Path
    spawn_id: SpawnId

    def in_root(self, runtime_root: Path | None) -> SpawnId | None:
        """The caller's spawn ID when ``runtime_root`` is the caller's own root."""

        if runtime_root is not None and same_runtime_root(self.runtime_root, runtime_root):
            return self.spawn_id
        return None


def same_runtime_root(a: Path, b: Path) -> bool:
    return a == b or a.resolve() == b.resolve()


def caller_spawn_id(environ: Mapping[str, str] | None = None) -> SpawnId | None:
    """The calling session's spawn ID without its root, for text that only names it."""

    raw = (os.environ if environ is None else environ).get("MERIDIAN_SPAWN_ID", "").strip()
    return SpawnId(raw) if raw else None


def caller_runtime_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """The runtime root of the project the calling environment names, if it names one:
    ``ResolvedContext``'s rule, through the same function.

    The process's own environment may name no project (a session started outside Meridian
    startup, as in tests): the caller is then in the project this process runs from.
    """

    from meridian.lib.config.project_root import resolve_project_root_resolution
    from meridian.lib.state.runtime_root import environment_runtime_root

    env = os.environ if environ is None else environ
    project = env.get("MERIDIAN_PROJECT_DIR", "").strip()
    if project:
        project_root = Path(project)
    elif environ is None:
        project_root = resolve_project_root_resolution().project_root
    else:
        project_root = None
    return environment_runtime_root(project_root, env)


def caller_identity(environ: Mapping[str, str] | None = None) -> CallerIdentity | None:
    """The calling session as (runtime root, spawn ID), or ``None`` outside a session.

    Under a ``-C`` to another project the CLI has removed both variables before any
    command runs, so the identity a command sees is the invoking project's or none.
    """

    spawn_id = caller_spawn_id(environ)
    runtime_root = caller_runtime_root(environ) if spawn_id is not None else None
    return CallerIdentity(runtime_root, spawn_id) if spawn_id and runtime_root else None


def caller_in(
    runtime_root: Path | None, environ: Mapping[str, str] | None = None
) -> SpawnId | None:
    """The calling session's spawn ID when ``runtime_root`` is the caller's own root."""

    identity = caller_identity(environ)
    return identity.in_root(runtime_root) if identity is not None else None


__all__ = [
    "CallerIdentity",
    "caller_identity",
    "caller_in",
    "caller_runtime_root",
    "caller_spawn_id",
    "same_runtime_root",
]
