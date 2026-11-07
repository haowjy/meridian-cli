"""Pi relocate: rewrite the transcript header's ``cwd`` after a project move.

Pi checks the header ``cwd`` on every open and exits when it is missing; nothing but
that first line needs to change. The rewrite goes through a same-folder temp file so
a crash leaves either the old file or the new one, never a torn header.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from meridian.lib.core.native_identity import (
    NativeEntryMismatch,
    NativeKeyFields,
    NativeSessionUnavailable,
)
from meridian.lib.harness.adapter import NativeRelocation, NativeRelocationResult
from meridian.lib.harness.pi_identity import read_header, resolve_session_file

_HEADER_VERSION = 3


def pi_native_cwd(identity: NativeKeyFields) -> Path | None:
    try:
        header = _header_for(identity)
    except (NativeSessionUnavailable, NativeEntryMismatch):
        return None
    cwd = header[1].get("cwd") if header is not None else None
    return Path(cwd) if isinstance(cwd, str) and cwd else None


def plan_pi_relocate(
    identity: NativeKeyFields, *, cwd_from: Path, cwd_to: Path
) -> NativeRelocation | None:
    def plan(change: str, source: Path | None, refusal: str | None = None) -> NativeRelocation:
        return NativeRelocation(identity, cwd_from, cwd_to, change, source=source, refusal=refusal)

    try:
        found = _header_for(identity)
    except (NativeSessionUnavailable, NativeEntryMismatch) as exc:
        return plan("Pi transcript header", None, f"cannot read the Pi transcript: {exc}")
    if found is None:  # pending session: no file yet, nothing recorded
        return None
    path, header = found
    if header.get("version") != _HEADER_VERSION or type(header.get("version")) is not int:
        return plan(
            "Pi header cwd",
            path,
            f"{path}: unsupported Pi header version {header.get('version')!r}",
        )
    return plan(f"Pi header cwd {cwd_from} -> {cwd_to} in {path}", path)


def apply_pi_relocations(
    plans: Sequence[NativeRelocation],
) -> tuple[NativeRelocationResult, ...]:
    return tuple(NativeRelocationResult(plan, _apply_one(plan)) for plan in plans)


def _apply_one(plan: NativeRelocation) -> str | None:
    path = plan.source
    assert path is not None  # a plan without a refusal names the transcript
    try:
        # Validate the file as it is now, not as it was at plan time.
        with path.open("rb") as handle:
            first = handle.readline()
        parsed: object = json.loads(first)
        if not isinstance(parsed, dict):
            return f"{path}: header changed since the plan"
        header = cast("dict[str, object]", parsed)
        cwd = header.get("cwd")
        if (
            header.get("type") != "session"
            or header.get("id") != plan.identity.session_id
            or header.get("version") != _HEADER_VERSION
            or not isinstance(cwd, str)
            or Path(cwd) not in (plan.cwd_from, plan.cwd_to)
        ):
            return f"{path}: header changed since the plan"
        if Path(cwd) == plan.cwd_to:
            return None  # already done
        header["cwd"] = str(plan.cwd_to)
        newline = b"\n" if first.endswith(b"\n") else b""
        line = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode() + newline
        _replace_first_line(path, skip=len(first), line=line)
    except (OSError, ValueError) as exc:
        return f"{path}: {exc}"
    return None


def _replace_first_line(path: Path, *, skip: int, line: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".relocate")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as out, path.open("rb") as source:
            out.write(line)
            source.seek(skip)
            shutil.copyfileobj(source, out)
            out.flush()
            os.fchmod(out.fileno(), path.stat().st_mode & 0o7777)
            os.fsync(out.fileno())
        temp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _header_for(identity: NativeKeyFields) -> tuple[Path, dict[str, object]] | None:
    if identity.native_store is None or identity.session_id is None:
        return None
    path = resolve_session_file(Path(identity.native_store), identity.session_id, pending=True)
    return None if path is None else (path, read_header(path))
