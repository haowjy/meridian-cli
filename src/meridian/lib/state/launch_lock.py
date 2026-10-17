"""Runner-lifetime flock, independent of the reentrant mutation locks.

Close descriptors to release: LOCK_UN would also unlock a background worker's
inherited open-file description. Never unlink or replace a published lock.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from meridian.lib.core.types import SpawnId

HANDOFF_ENV = "_MERIDIAN_LAUNCH_LOCK_FD"


class Busy(ValueError):
    """An exclusive owner already holds the launch."""


class Contended(ValueError):
    """Readers prevented acquisition; retry, not evidence of a live runner."""


class NotFound(ValueError):
    """The published spawn or its lock no longer exists."""


@contextmanager
def shared_hold(path: Path) -> Generator[bool | None]:
    """Try a shared lock and keep it held across the caller's read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        yield None
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            result = False
        else:
            result = True
        yield result
    finally:
        os.close(fd)


def shared_try(path: Path) -> bool | None:
    """True if free of writers, False if exclusively held, None if lockless."""
    with shared_hold(path) as result:
        return result


def _validate(fd: int, spawn_dir: Path) -> None:
    try:
        opened = os.fstat(fd)
        published = (spawn_dir / "launch.lock").stat()
        same = (opened.st_dev, opened.st_ino) == (published.st_dev, published.st_ino)
        if same and (spawn_dir / "state.json").is_file():
            return
    except FileNotFoundError:
        pass
    raise NotFound(f"Spawn '{spawn_dir.name}' no longer exists")


@dataclass(frozen=True)
class LaunchHandle(AbstractContextManager["LaunchHandle"]):
    """Exclusive launch capability, valid for its owner's lexical lifetime."""

    spawn_id: SpawnId
    fd: int

    def close(self) -> None:
        os.close(self.fd)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class LaunchSlot(AbstractContextManager["LaunchSlot"]):
    """Own a staged capability before the spawn ID has been allocated."""

    def __init__(self) -> None:
        self._handle: LaunchHandle | None = None

    @property
    def handle(self) -> LaunchHandle:
        if self._handle is None:
            raise RuntimeError("Launch slot is not bound")
        return self._handle

    def bind_staged(self, spawn_id: SpawnId, stage: Path) -> LaunchHandle:
        if self._handle is not None:
            raise RuntimeError("Launch slot already bound")
        fd = os.open(
            stage / "launch.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._handle = LaunchHandle(spawn_id, fd)
        return self._handle

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def acquire(spawn_dir: Path) -> LaunchHandle:
    """Acquire a published launch, distinguishing runners from brief readers."""
    path = spawn_dir / "launch.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise NotFound(f"Spawn '{spawn_dir.name}' has no launch lock") from exc
    deadline = time.monotonic() + 1.0
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                readers_only = shared_try(path)
                if readers_only is None:
                    raise NotFound(f"Spawn '{spawn_dir.name}' no longer exists") from None
                if not readers_only:
                    raise Busy(SpawnId(spawn_dir.name)) from None
                if time.monotonic() >= deadline:
                    raise Contended(f"{spawn_dir.name} launch lock is contended; retry") from None
                time.sleep(0.01)
        _validate(fd, spawn_dir)
        return LaunchHandle(SpawnId(spawn_dir.name), fd)
    except BaseException:
        os.close(fd)
        raise


def take_handoff_fd() -> int:
    """Worker's first act: stop inheritance before doing any startup work."""
    fd = int(os.environ.pop(HANDOFF_ENV))
    os.set_inheritable(fd, False)
    return fd


def adopt_handoff(fd: int, spawn_dir: Path) -> LaunchHandle:
    """Validate the inherited description and publish this worker's identity."""
    from meridian.lib.state.spawn_store import update_spawn

    handle = LaunchHandle(SpawnId(spawn_dir.name), fd)
    try:
        _validate(fd, spawn_dir)
        update_spawn(spawn_dir.parent.parent, spawn_dir.name, runner_pid=os.getpid())
    except BaseException:
        handle.close()
        raise
    return handle
