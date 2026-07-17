from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.hooks.builtin.autosync_store import write_sync_state

if TYPE_CHECKING:
    import pytest


def test_concurrent_sync_state_writers_do_not_share_a_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two writers must each own the temporary file they later replace."""

    replace_barrier = threading.Barrier(2)
    original_replace = Path.replace

    def synchronized_replace(source: Path, target: Path) -> Path:
        if source.name == "state.json.tmp":
            replace_barrier.wait(timeout=5)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", synchronized_replace)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(write_sync_state, tmp_path, outcome=outcome)
            for outcome in ("first", "second")
        ]
        for future in futures:
            future.result()

    payload = json.loads(
        (tmp_path / ".meridian" / "autosync" / "state.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["outcome"] in {"first", "second"}
