"""Machine-wide spawn and chat IDs: one symlink per issued ID under ``machine/ids/``.

A claim is a symlink named for the ID that points at the runtime root the ID belongs
to. ``symlink(2)`` is atomic and exclusive, so the claim is the only authority: a
crash leaves no claim or a whole one. Claims are never rewritten or deleted. Layout and
rationale: ``../.context/CONTEXT.md``.
"""

import os
import re
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Final, Literal

from meridian.lib.platform.atomic import atomic_write_text, fsync_directory
from meridian.lib.state.user_paths import get_user_home

type IdKind = Literal["p", "c"]

FORMAT_VERSION: Final = "1"
# After this many lost races, list the claims again instead of adding one each time.
_RELIST_AFTER: Final = 8
_BUCKET_SIZE: Final = 1000
_CLAIM_ID: Final = re.compile(r"[pc](0|[1-9][0-9]*)", re.ASCII)
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

    if _CLAIM_ID.fullmatch(claim_id) is None:
        return None
    link = _claim_path(ids_dir(), claim_id[0], int(claim_id[1:]))
    try:
        target = os.readlink(link)
    except (FileNotFoundError, PermissionError):
        # A claim directory hidden from a sandbox reads as no claim: look in the own project.
        return None
    return Path(os.path.normpath(os.path.join(link.parent, target)))


def allocate(kind: IdKind, runtime_root: Path) -> str:
    """Issue the next ``kind`` ID for ``runtime_root`` and return it (``p20001``).

    The number is above the highest claim and above ``runtime_root``'s own legacy
    counter. ``symlink(2)`` fails if the name is taken, so concurrent allocators need
    no lock: whoever loses an ``EEXIST`` tries a higher number.
    """

    root = ids_dir()
    _check_format(root)
    highest = _highest(root, kind)
    if highest is None:
        seed = _seed(kind, runtime_root)
        # Another allocator may have claimed while the seed was computed: its claims win.
        highest = _highest(root, kind)
        if highest is None:
            highest = seed - 1
    number = max(highest, _counter_in_root(kind, runtime_root)) + 1
    attempt = 0
    while True:
        try:
            _create_claim(root, kind, number, runtime_root)
        except FileExistsError:
            attempt += 1
            number += 1
            if attempt % _RELIST_AFTER == 0:
                number = max(number, (_highest(root, kind) or 0) + 1)
            continue
        return f"{kind}{number}"


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


def lowest_claim(kind: IdKind) -> int | None:
    """The lowest number of ``kind`` that has a claim, ``None`` when there is none.

    Every ID of ``kind`` below it was issued before claims; one above it with no claim
    was never issued.
    """

    kind_dir = ids_dir() / kind
    try:
        buckets = sorted(int(n) for n in os.listdir(kind_dir) if n.isdecimal())
    except (FileNotFoundError, PermissionError):
        return None
    for bucket in buckets:
        numbers = numbered_names(kind_dir / str(bucket), kind)
        if numbers:
            return numbers[-1]
    return None


def recent_claim_roots(max_age_seconds: float) -> set[Path]:
    """The runtime roots of claims made in the last ``max_age_seconds``.

    Looks only at the two newest buckets of each kind, skipping a bucket whose directory
    has not changed in that time, and never follows a link.
    """

    cutoff = time.time() - max_age_seconds
    roots: set[Path] = set()
    for kind in ("p", "c"):
        kind_dir = ids_dir() / kind
        try:
            buckets = sorted((int(n) for n in os.listdir(kind_dir) if n.isdecimal()), reverse=True)
        except (FileNotFoundError, PermissionError):
            continue
        for bucket in buckets[:2]:
            bucket_dir = kind_dir / str(bucket)
            try:
                if bucket_dir.stat().st_mtime < cutoff:
                    continue
                with os.scandir(bucket_dir) as entries:
                    links = [e for e in entries if e.stat(follow_symlinks=False).st_mtime >= cutoff]
            except (FileNotFoundError, PermissionError):
                continue
            for link in links:
                target = claim_root(link.name)
                if target is not None:
                    roots.add(target)
    return roots


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


def _counter_in_root(kind: IdKind, root: Path) -> int:
    """The root's legacy counter for ``kind``, 0 when absent or unreadable."""

    name = "spawn-id-counter" if kind == "p" else "session-id-counter"
    with suppress(OSError, ValueError):
        return int((root / name).read_text(encoding="utf-8").strip())
    return 0


def _used_in_root(kind: IdKind, root: Path) -> int:
    directory = root / ("spawns" if kind == "p" else "sessions")
    return max(max(numbered_names(directory, kind), default=0), _counter_in_root(kind, root))


__all__ = [
    "MachineIdError",
    "allocate",
    "claim_root",
    "ids_dir",
    "lowest_claim",
    "numbered_names",
    "recent_claim_roots",
]
