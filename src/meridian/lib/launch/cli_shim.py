"""Make ``meridian`` on a harness's PATH resolve to the build that launched it.

Code inside a harness (plugin hooks, Pi extensions, Mars hooks, the model's own
shell) reaches Meridian by the name ``meridian``. When another build is first on
PATH, that code talks to the wrong one. The fix lives in the child PATH, so no
caller needs to change.
"""

import hashlib
import shlex
import shutil
import sys
from collections.abc import Collection
from pathlib import Path

from meridian.lib.platform.atomic import AtomicReplaceDurabilityError, atomic_replace
from meridian.lib.state.user_paths import get_user_home


def _within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _shim_text() -> str:
    # ``-P`` keeps a project's own ``meridian/`` directory from shadowing the package.
    return f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -P -m meridian "$@"\n'


def pin_launching_build(env: dict[str, str], *, projected_roots: Collection[Path]) -> str | None:
    """Put a ``meridian`` shim for this build first on ``env["PATH"]`` when needed.

    Does nothing when ``meridian`` on that PATH already is this build (release and
    ``uv run`` launches). Returns a warning when it had to leave PATH unchanged.
    The shim lives under the user home, which sandboxes do not project writable: a
    shim a sandboxed agent could rewrite would run unsandboxed through
    ``meridian spawn``.
    """

    path = env.get("PATH")
    if not path:
        return None
    digest = hashlib.sha256(sys.executable.encode()).hexdigest()[:16]
    shim_dir = get_user_home(env=env) / "bin" / digest
    shim = shim_dir / "meridian"
    found = shutil.which("meridian", path=path)
    own = {(Path(sys.executable).parent / "meridian").resolve(), shim.resolve()}
    if found and Path(found).resolve() in own:
        return None
    if any(_within(shim_dir, root) for root in projected_roots):
        return (
            f"Not pinning `meridian` to this build: {shim_dir} is inside a writable "
            "sandbox root. Set MERIDIAN_HOME outside /tmp and the project."
        )
    text = _shim_text()
    try:
        try:
            current = shim.read_text()
        except FileNotFoundError:
            current = None
        if current != text:
            with atomic_replace(shim, permissions=0o755) as handle:
                handle.write(text)
    except AtomicReplaceDurabilityError:
        pass  # The rename committed; only the directory fsync failed.
    except OSError as error:
        return f"Not pinning `meridian` to this build: cannot write {shim}: {error}"
    shim_dir_str = shim_dir.as_posix()
    kept = [entry for entry in path.split(":") if entry.strip() and entry != shim_dir_str]
    env["PATH"] = ":".join([shim_dir_str, *kept])
    return None
