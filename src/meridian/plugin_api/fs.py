"""Filesystem primitives exposed to plugins."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from meridian.lib.platform.locking import lock_file


@contextmanager
def file_lock(
    path: Path | str,
    *,
    timeout: float = 60.0,
    mode: Literal["exclusive", "shared"] = "exclusive",
) -> Generator[None, None, None]:
    """Acquire a cross-platform file lock with timeout.

    Raises:
        TimeoutError: If the lock cannot be acquired before timeout expires.
    """

    with lock_file(Path(path), timeout=timeout, mode=mode, reentrant=False) as handle:
        if mode == "exclusive":
            # PID in lock file is useful for lock contention debugging.
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("utf-8"))
            handle.flush()

        yield
