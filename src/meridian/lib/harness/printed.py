"""A launch Meridian prints for the person to run, because it may not exec the harness.

Stdout is one shell line (``(cd DIR && agy ...)``, so ``eval`` leaves the caller's shell
where it was). A command that is long or holds a newline is written to a private script
under the harness's state directory and stdout is ``sh <script>``: some shells and the
agents that paste the line cap its length. ``env`` is only what the harness projects on top
of the caller's environment, never the child environment Meridian bound.
"""

from __future__ import annotations

import os
import shlex
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.util import FormatContext
from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text

_ONE_LINER_MAX_CHARS = 240
_SCRIPT_MAX_AGE_SECONDS = 7 * 24 * 3600
EVAL_HINT = 'eval "$(meridian agy ...)" runs it in your shell'


class PrintedLaunch(BaseModel):
    """``--json`` is this model; text output is ``command``."""

    model_config = ConfigDict(frozen=True)

    command: str
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    script: str | None = None
    warnings: tuple[str, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return self.command


def print_launch(
    *,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    warnings: Sequence[str],
    script_dir: Path,
) -> PrintedLaunch:
    words = shlex.join([*(f"{key}={value}" for key, value in env.items()), *argv])
    where = shlex.quote(cwd.as_posix())
    one_liner = f"(cd {where} && {words})"
    script: Path | None = None
    command = one_liner
    if len(one_liner) > _ONE_LINER_MAX_CHARS or "\n" in one_liner:
        script = _write_script(script_dir, f"cd {where} && exec {'env ' if env else ''}{words}\n")
        command = f"sh {shlex.quote(script.as_posix())}"
    return PrintedLaunch(
        command=command,
        argv=tuple(argv),
        cwd=cwd.as_posix(),
        env=dict(env),
        script=script.as_posix() if script is not None else None,
        warnings=tuple(warnings),
    )


def _write_script(script_dir: Path, text: str) -> Path:
    script_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prune(script_dir)
    path = script_dir / f"{uuid.uuid4().hex[:12]}.sh"
    atomic_write_text(path, text, durable=False, permissions=PRIVATE_FILE_MODE)
    return path


def _prune(script_dir: Path) -> None:
    cutoff = time.time() - _SCRIPT_MAX_AGE_SECONDS
    for path in script_dir.glob("*.sh"):
        try:
            if path.stat().st_mtime < cutoff:
                os.unlink(path)
        except FileNotFoundError:
            continue  # another run pruned it first


__all__ = ["EVAL_HINT", "PrintedLaunch", "print_launch"]
