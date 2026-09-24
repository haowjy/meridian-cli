"""Exact, descriptor-validated local source qualification for Pi RPC state."""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from meridian.lib.state.session_authority import (
    LocalObjectStamp,
    PendingLocalFile,
    QualifiedLocalFile,
)

SourceFailure = Literal[
    "missing", "inaccessible", "store_changed", "file_changed",
    "identity_mismatch", "invalid_native_source",
]


@dataclass(frozen=True)
class PiSourceQualified:
    observation: QualifiedLocalFile


@dataclass(frozen=True)
class PiSourcePending:
    observation: PendingLocalFile


@dataclass(frozen=True)
class PiSourceUnavailable:
    reason: SourceFailure


type PiSourceObservation = PiSourceQualified | PiSourcePending | PiSourceUnavailable


@dataclass(frozen=True)
class PiSourcePreflight:
    status: Literal["eligible", "unavailable"]
    reason: SourceFailure | None = None


def qualify_pi_source(
    *, effective_store: Path, session_id: str, session_file: str
) -> PiSourceObservation:
    """Qualify only the owner-reported Pi path against its effective store.

    Symlink aliases are resolved once during acquisition. Header identity and
    object stamps are read from the same descriptor opened via no-follow
    directory traversal. An absent selected file is pending, not discovery.
    """
    if not session_id.strip() or not session_file.strip():
        return PiSourceUnavailable("invalid_native_source")
    try:
        root = effective_store.expanduser().resolve(strict=True)
        root_stat = root.stat()
        if not stat.S_ISDIR(root_stat.st_mode):
            return PiSourceUnavailable("invalid_native_source")
        selected = Path(session_file).expanduser()
        if not selected.is_absolute():
            return PiSourceUnavailable("invalid_native_source")
        try:
            canonical = selected.resolve(strict=True)
        except FileNotFoundError:
            canonical = _canonical_pending_path(selected)
            if not _descendant(canonical, root):
                return PiSourceUnavailable("invalid_native_source")
            with _open_directory(root) as root_fd:
                if _stat_stamp(root_fd) != _stamp(root_stat):
                    return PiSourceUnavailable("store_changed")
                if not _walk_parent(root_fd, root, canonical.parent, canonical.name):
                    return PiSourceUnavailable("file_changed")
            return PiSourcePending(
                PendingLocalFile(
                    kind="local_file_pending",
                    path=str(canonical),
                    store_object=_stamp(root_stat),
                )
            )
        if not _descendant(canonical, root):
            return PiSourceUnavailable("invalid_native_source")
        with _open_directory(root) as root_fd:
            if _stat_stamp(root_fd) != _stamp(root_stat):
                return PiSourceUnavailable("store_changed")
            file_fd = _open_file(root_fd, root, canonical)
            try:
                file_stat = os.fstat(file_fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    return PiSourceUnavailable("invalid_native_source")
                header = _read_header(file_fd)
                if header is None:
                    return PiSourceUnavailable("invalid_native_source")
                if header.get("id") != session_id:
                    return PiSourceUnavailable("identity_mismatch")
                if _stat_stamp(root_fd) != _stamp(root_stat):
                    return PiSourceUnavailable("store_changed")
                if _stat_stamp(file_fd) != _stamp(file_stat):
                    return PiSourceUnavailable("file_changed")
            finally:
                os.close(file_fd)
        if selected.resolve(strict=True) != canonical:
            return PiSourceUnavailable("file_changed")
        if effective_store.expanduser().resolve(strict=True) != root:
            return PiSourceUnavailable("store_changed")
        return PiSourceQualified(
            QualifiedLocalFile(
                kind="local_file",
                path=str(canonical),
                store_object=_stamp(root_stat),
                file_object=_stamp(file_stat),
                rule="pi_rpc_exact_v1",
            )
        )
    except OSError as exc:
        return PiSourceUnavailable(_os_failure(exc))
    except (ValueError, RuntimeError):
        return PiSourceUnavailable("invalid_native_source")


def preflight_pi_source(
    source: QualifiedLocalFile, *, effective_store: Path, session_id: str
) -> PiSourcePreflight:
    """Revalidate the saved exact path and object stamps; never resolve aliases."""
    path = Path(source.path)
    # The native key's recorded store is supplied by the authority resolver;
    # do not use today's Pi default or canonicalize the pinned file anew.
    root = effective_store
    if not root.is_absolute() or not path.is_absolute() or not _descendant(path, root):
        return PiSourcePreflight("unavailable", "invalid_native_source")
    try:
        with _open_directory(root) as root_fd:
            if _stat_stamp(root_fd) != source.store_object:
                return PiSourcePreflight("unavailable", "store_changed")
            file_fd = _open_file(root_fd, root, path)
            try:
                if _stat_stamp(file_fd) != source.file_object:
                    return PiSourcePreflight("unavailable", "file_changed")
                if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                    return PiSourcePreflight("unavailable", "invalid_native_source")
                header = _read_header(file_fd)
                if header is None:
                    return PiSourcePreflight("unavailable", "invalid_native_source")
                if header.get("id") != session_id:
                    return PiSourcePreflight("unavailable", "identity_mismatch")
                if _stat_stamp(root_fd) != source.store_object:
                    return PiSourcePreflight("unavailable", "store_changed")
                if _stat_stamp(file_fd) != source.file_object:
                    return PiSourcePreflight("unavailable", "file_changed")
            finally:
                os.close(file_fd)
    except OSError as exc:
        return PiSourcePreflight("unavailable", _os_failure(exc))
    return PiSourcePreflight("eligible")


def _descendant(path: Path, root: Path) -> bool:
    try:
        return path != root and path.is_relative_to(root)
    except ValueError:
        return False


def _stamp(value: os.stat_result) -> LocalObjectStamp:
    return LocalObjectStamp(device=value.st_dev, inode=value.st_ino)


def _stat_stamp(fd: int) -> LocalObjectStamp:
    return _stamp(os.fstat(fd))


class _DirectoryFd:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __enter__(self) -> int:
        return self.fd

    def __exit__(self, *_: object) -> None:
        os.close(self.fd)


def _open_directory(path: Path) -> _DirectoryFd:
    if not path.is_absolute():
        raise ValueError("directory path must be absolute")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = nxt
        return _DirectoryFd(current)
    except BaseException:
        os.close(current)
        raise


def _relative_parts(root: Path, path: Path) -> tuple[str, ...]:
    if not _descendant(path, root):
        raise ValueError("source path escapes store")
    parts = path.relative_to(root).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid source path components")
    return parts


def _walk_parent(root_fd: int, root: Path, parent: Path, leaf: str) -> bool:
    parts = _relative_parts(root, parent / "placeholder")[:-1]
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                nxt = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
            except FileNotFoundError:
                return True
            os.close(current)
            current = nxt
        try:
            os.stat(leaf, dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    finally:
        os.close(current)


def _canonical_pending_path(path: Path) -> Path:
    missing: list[str] = []
    candidate = path
    while True:
        try:
            resolved = candidate.resolve(strict=True)
            break
        except FileNotFoundError:
            if candidate == candidate.parent:
                raise
            missing.append(candidate.name)
            candidate = candidate.parent
    for component in reversed(missing):
        resolved /= component
    return resolved


def _open_file(root_fd: int, root: Path, path: Path) -> int:
    parts = _relative_parts(root, path)
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
    finally:
        os.close(current)


def _read_header(fd: int) -> dict[str, object] | None:
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), "rb") as stream:
        first_line = stream.readline(1024 * 1024 + 1)
    if len(first_line) > 1024 * 1024:
        return None
    try:
        value = json.loads(first_line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    header = cast("dict[str, object]", value)
    if header.get("type") != "session":
        return None
    if not isinstance(header.get("id"), str) or not header["id"]:
        return None
    return header


def _os_failure(exc: OSError) -> SourceFailure:
    if exc.errno == errno.ENOENT:
        return "missing"
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return "inaccessible"
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return "file_changed"
    return "invalid_native_source"
