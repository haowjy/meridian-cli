from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.hooks.builtin import autosync_store
from meridian.lib.hooks.builtin.autosync_store import append_conflict_notice, write_sync_state
from meridian.plugin_api.fs import AtomicReplaceDurabilityError

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


def test_boolean_writer_reconciles_committed_durability_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# Instructions\n", encoding="utf-8")

    def commit_then_fail(path: Path, content: str, *, durable: bool = True) -> None:
        del durable
        path.write_text(content, encoding="utf-8")
        raise AtomicReplaceDurabilityError(path)

    monkeypatch.setattr(autosync_store, "atomic_write_text", commit_then_fail)

    assert append_conflict_notice(tmp_path, "c1", ["file.txt"], "main") is True
    assert "autosync-conflict:c1" in agents_md.read_text(encoding="utf-8")
