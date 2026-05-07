"""Unit tests for persisted active-work pointer state."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state.current_work import get_current_work_id, set_current_work_id


def test_set_and_get_current_work_id_round_trip(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    assert get_current_work_id(runtime_root) is None

    changed = set_current_work_id(runtime_root, "work-1")
    assert changed
    assert get_current_work_id(runtime_root) == "work-1"

    unchanged = set_current_work_id(runtime_root, "work-1")
    assert not unchanged

    cleared = set_current_work_id(runtime_root, None)
    assert cleared
    assert get_current_work_id(runtime_root) is None


def test_get_current_work_id_ignores_invalid_payload(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "current-work.json").write_text("not-json", encoding="utf-8")

    assert get_current_work_id(runtime_root) is None
