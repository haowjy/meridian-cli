"""Make ``meridian`` on a harness's PATH resolve to the build that launched it.

Code inside a harness (plugin hooks, Pi extensions, Mars hooks, the model's own
shell) reaches Meridian by the name ``meridian``. When another build is first on
PATH, that code talks to the wrong one. The fix lives in the child PATH, so no
caller needs to change.

Two halves: ``pin_launching_build`` decides and edits PATH at bind (pure, runs in
previews too); ``write_pinned_shim`` writes the file at materialize (real launches).
"""

import hashlib
import shlex
import shutil
import sys
from collections.abc import Collection, Mapping
from pathlib import Path

import structlog

from meridian.lib.platform.atomic import atomic_replace
from meridian.lib.state.user_paths import get_user_home

from .env import path_within

logger = structlog.get_logger(__name__)


def _shim_location(env: Mapping[str, str]) -> tuple[Path, str]:
    # ``-P`` keeps a project's own ``meridian/`` directory from shadowing the package.
    # The directory is named by the shim's text, so a format change gets a new one.
    text = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -P -m meridian "$@"\n'
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return (get_user_home(env=env) / "bin" / digest).resolve() / "meridian", text


def pin_launching_build(env: dict[str, str], *, write_roots: Collection[Path]) -> str | None:
    """Put the shim's directory first on ``env["PATH"]`` when ``meridian`` there is another build.

    ``write_roots`` is what a sandboxed agent can write. The shim lives under the user
    home, outside them: a shim an agent could rewrite would run unsandboxed through
    ``meridian spawn``. Returns a warning when it refuses. Writes nothing.
    """

    path = env.get("PATH")
    if not path:
        return None
    shim, _ = _shim_location(env)
    found = shutil.which("meridian", path=path)
    resolved = Path(found).resolve() if found and Path(found).is_absolute() else None
    if resolved == (Path(sys.executable).parent / "meridian").resolve():
        return None
    # /tmp is writable even when TMPDIR moved it out of the projected roots.
    if any(path_within(shim, root) for root in (*write_roots, Path("/tmp"))):
        return (
            f"Not pinning `meridian` to this build: {shim.parent} is inside a writable "
            "sandbox root. Set MERIDIAN_HOME outside /tmp and the project."
        )
    if resolved == shim:
        return None
    env["PATH"] = f"{shim.parent.as_posix()}:{path}"
    return None


def write_pinned_shim(env: Mapping[str, str]) -> None:
    """Write the shim when ``pin_launching_build`` put its directory first on PATH."""

    shim, text = _shim_location(env)
    if env.get("PATH", "").split(":", 1)[0] != shim.parent.as_posix() or shim.exists():
        return
    try:
        with atomic_replace(shim, permissions=0o755) as handle:
            handle.write(text)
    except OSError as error:
        logger.warning("cli_shim_write_failed", path=str(shim), error=str(error))
