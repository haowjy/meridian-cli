"""``--continue`` into another project: run the same command inside that project.

A continued session belongs to its own project: its config, its work items and its
harness's per-directory session store. So a claimed ID that names a session in another
project re-runs the command as ``meridian -C <that project> ...``, through the same
environment scrub as a typed ``-C``: the caller's spawn, chat and work names are gone,
and the continued session gets no parent. The depth guard's counter is not
project-scoped, so it rides along in the environment.
"""

import os
from collections.abc import Sequence
from pathlib import Path

from meridian.lib.core.caller import same_runtime_root
from meridian.lib.core.refs import RefNotFound, parse_ref, project_named_scope
from meridian.lib.ops.locate import (
    Untracked,
    local_ref,
    locate,
    require_project_dir,
)
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.platform.python_module import module_argv
from meridian.lib.state.machine_ids import claim_root

_CONTINUE_COMMANDS = ((), ("spawn",))


def continue_ref(argv: Sequence[str]) -> str | None:
    """The reference after ``--continue``, or ``None`` when it has none."""

    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == "--continue":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token.startswith("--continue="):
            return token.partition("=")[2] or None
    return None


def reenter_project_for_continue(
    argv: Sequence[str],
    *,
    command_path: tuple[str, ...] | None,
    project_root: Path,
    directory_explicit: bool,
) -> None:
    """Exec the command under ``-C`` when ``--continue`` names a session in another project.

    Returns when the session is in this project (or isn't a claimed ID, which looks in
    the project it is run from). Raises ``ValueError`` when ``-C`` names a project other
    than the session's, the session's project directory is gone, or the reference names
    another machine.
    """

    ref = continue_ref(argv) if command_path in _CONTINUE_COMMANDS else None
    if ref is None:
        return
    # `<this machine>/<id>` is the bare ID; another machine's name raises here as it would
    # in the launch.
    parsed = local_ref(parse_ref(ref))
    if parsed.kind not in ("spawn", "chat"):
        return
    # Most continues are in the project they run from: one readlink settles that, before
    # any journal is read.
    claimed = claim_root(parsed.value)
    own = resolve_runtime_root_for_read(project_root)
    if claimed is None or (own is not None and same_runtime_root(claimed, own)):
        return
    try:
        with project_named_scope(directory_explicit):
            hit = locate(project_root, ref)
    except RefNotFound:
        return  # the launch reports it, with its own wording
    if isinstance(hit, Untracked) or (own is not None and same_runtime_root(hit.runtime_root, own)):
        return
    target = require_project_dir(hit.project_root, ref)
    if directory_explicit:
        raise ValueError(
            f"{ref} belongs to the project at {target}, but -C names {project_root}. "
            "Drop -C: a session is continued in its own project."
        )
    relaunch = module_argv("meridian", "-C", str(target), *argv)
    os.execv(relaunch[0], relaunch)


__all__ = ["continue_ref", "reenter_project_for_continue"]
