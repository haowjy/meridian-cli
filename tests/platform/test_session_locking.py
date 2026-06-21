"""POSIX session-lock acquisition semantics.

Cross-process session-lock exclusivity is covered in
``tests/integration/state/test_session_concurrency.py``. This module holds
platform-specific retry behavior for lock-path replacement.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.lib.state import session_store
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest


@posix_only
def test_posix_acquire_session_lock_retries_when_lock_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "sessions" / "c123.lock"
    real_open = Path.open

    def _open_and_replace(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if self == lock_path and not replaced["done"]:
            replaced["done"] = True
            lock_path.unlink(missing_ok=True)
            lock_path.touch()
        return handle

    replaced = {"done": False}

    monkeypatch.setattr(Path, "open", _open_and_replace)

    handle = session_store._posix_acquire_session_lock(lock_path)
    try:
        assert replaced["done"] is True
        assert session_store._lock_handle_matches_path(handle, lock_path)
    finally:
        session_store._posix_release_session_lock(handle)
        handle.close()
