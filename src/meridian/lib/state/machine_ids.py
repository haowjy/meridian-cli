"""Machine-wide spawn and chat IDs: one symlink per issued ID under ``machine/ids/``.

A claim is a symlink named for the ID that points at the runtime root the ID belongs
to. ``symlink(2)`` is atomic and exclusive, so the claim is the only authority: a
crash leaves no claim or a whole one. Claims are never rewritten or deleted. Layout and
rationale: ``../.context/CONTEXT.md``.
"""

import os
import re
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Final, Literal

from meridian.lib.platform.atomic import atomic_write_text, fsync_directory
from meridian.lib.platform.locking import FileLockTimeout, lock_file
from meridian.lib.state.user_paths import get_user_home

type IdKind = Literal["p", "c"]

FORMAT_VERSION: Final = "1"
LOCK_TIMEOUT_SECONDS: Final = 5.0
_BUCKET_SIZE: Final = 1000
# Room left above every legacy number for older builds that are still running.
_SEED_HEADROOM: Final = 1000


class MachineIdError(RuntimeError):
    """No ID was issued."""


def ids_dir() -> Path:
    return get_user_home() / "machine" / "ids"


def numbered_names(directory: Path, kind: IdKind) -> list[int]:
    """Return the numbers of ``<kind><digits>`` entries in ``directory``, highest first."""

    pattern = re.compile(rf"{kind}([0-9]+)")
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted((int(m[1]) for n in names if (m := pattern.fullmatch(n))), reverse=True)


def claim_root(claim_id: str) -> Path | None:
    """Return the runtime root ``claim_id`` was issued for, or ``None`` without a claim.

    One ``readlink``; the link is never followed, so a pruned root still answers.
    """

    kind, digits = claim_id[:1], claim_id[1:]
    if kind not in ("p", "c") or not digits.isdecimal():
        return None
    link = _claim_path(ids_dir(), kind, int(digits))
    try:
        target = os.readlink(link)
    except FileNotFoundError:
        return None
    return Path(os.path.normpath(os.path.join(link.parent, target)))


def allocate(kind: IdKind, runtime_root: Path) -> str:
    """Issue the next ``kind`` ID for ``runtime_root`` and return it (``p20001``).

    ``ids.lock`` is the innermost lock and is held only while a number is chosen.
    """

    root = ids_dir()
    _check_format(root)
    seed = _seed(kind, runtime_root) if _highest(root, kind) is None else None
    lock_path = root / "ids.lock"
    try:
        with lock_file(lock_path, timeout=LOCK_TIMEOUT_SECONDS):
            highest = _highest(root, kind)
            if highest is not None:
                number = highest + 1
            else:
                number = seed if seed is not None else _seed(kind, runtime_root)
            while True:
                try:
                    _create_claim(root, kind, number, runtime_root)
                except FileExistsError:
                    number += 1
                    continue
                return f"{kind}{number}"
    except FileLockTimeout as exc:
        raise MachineIdError(
            f"Timed out after {LOCK_TIMEOUT_SECONDS:g}s waiting for {lock_path}: "
            "another Meridian process holds the ID lock (stopped or stuck?)."
        ) from exc


def _claim_path(root: Path, kind: str, number: int) -> Path:
    return root / kind / str(number // _BUCKET_SIZE) / f"{kind}{number}"


def _check_format(root: Path) -> None:
    format_file = root / "format"
    try:
        found = format_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(format_file, f"{FORMAT_VERSION}\n")
        return
    if found != FORMAT_VERSION:
        raise MachineIdError(
            f"{format_file} says format {found!r}, which this Meridian does not know "
            f"(it knows {FORMAT_VERSION!r}): a newer Meridian wrote it. Upgrade Meridian."
        )


def _highest(root: Path, kind: IdKind) -> int | None:
    kind_dir = root / kind
    try:
        buckets = sorted((int(n) for n in os.listdir(kind_dir) if n.isdecimal()), reverse=True)
    except FileNotFoundError:
        return None
    for bucket in buckets:
        numbers = numbered_names(kind_dir / str(bucket), kind)
        if numbers:
            return numbers[0]
    return None


def _create_claim(root: Path, kind: IdKind, number: int, runtime_root: Path) -> None:
    link = _claim_path(root, kind, number)
    bucket = link.parent
    bucket_is_new = not bucket.is_dir()
    bucket.mkdir(parents=True, exist_ok=True)
    os.symlink(_claim_target(bucket, runtime_root), link)
    fsync_directory(bucket)
    if bucket_is_new:
        fsync_directory(bucket.parent)


def _claim_target(bucket: Path, runtime_root: Path) -> str:
    runtime_root = Path(os.path.abspath(runtime_root))
    projects = get_user_home() / "projects"
    if runtime_root.is_relative_to(projects):
        return os.path.relpath(runtime_root, bucket)
    return str(runtime_root)


def _seed(kind: IdKind, runtime_root: Path) -> int:
    """First number to issue on a machine with no claim of ``kind``.

    Above every number any runtime root has used, rounded up to a thousand with a
    thousand more, so an older build still running, or a rollback, has room: even a
    first-ever allocation starts at 1000.
    """

    used = max((_used_in_root(kind, r) for r in _runtime_roots(runtime_root)), default=0)
    return -(-used // _BUCKET_SIZE) * _BUCKET_SIZE + _SEED_HEADROOM


def _runtime_roots(runtime_root: Path) -> Iterator[Path]:
    yield runtime_root
    with suppress(FileNotFoundError):
        for entry in os.scandir(get_user_home() / "projects"):
            if entry.is_dir():
                yield Path(entry.path)


def _used_in_root(kind: IdKind, root: Path) -> int:
    counter, directory = (
        ("spawn-id-counter", root / "spawns")
        if kind == "p"
        else ("session-id-counter", root / "sessions")
    )
    used = max(numbered_names(directory, kind), default=0)
    with suppress(OSError, ValueError):
        used = max(used, int((root / counter).read_text(encoding="utf-8").strip()))
    return used


__all__ = ["MachineIdError", "allocate", "claim_root", "ids_dir", "numbered_names"]
