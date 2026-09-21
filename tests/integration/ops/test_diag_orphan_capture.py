"""Orphan-run repair captures the native transcript of a reconciled primary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from meridian.lib.ops import diag
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn.model import SpawnKind, SpawnRecord

if TYPE_CHECKING:
    from pathlib import Path


def _install_reconciled_orphan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: SpawnKind,
) -> None:
    running = SpawnRecord(id="p1", kind=kind, status="running")
    terminal = SpawnRecord(id="p1", kind=kind, status="cancelled")
    monkeypatch.setattr(
        spawn_store,
        "list_spawns",
        lambda _root: SimpleNamespace(records=(running,)),
    )
    monkeypatch.setattr(
        "meridian.lib.state.reaper.reconcile_active_spawn",
        lambda *_args, **_kwargs: terminal,
    )


def test_reconciled_primary_orphan_is_captured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reconciled_orphan(monkeypatch, kind="primary")
    captured: list[tuple[Path, Path, str]] = []
    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        lambda *args: captured.append(args),
    )

    runtime_root = tmp_path / "runtime"
    count, ids = diag._repair_orphan_runs(tmp_path, runtime_root=runtime_root)

    assert count == 1
    assert ids == ("p1",)
    assert captured == [(tmp_path, runtime_root, "p1")]


def test_reconciled_child_orphan_is_not_captured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reconciled_orphan(monkeypatch, kind="child")
    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        lambda *_args: pytest.fail("only primaries capture native history"),
    )

    count, ids = diag._repair_orphan_runs(tmp_path, runtime_root=tmp_path / "runtime")

    assert count == 1
    assert ids == ("p1",)


def test_orphan_capture_failure_does_not_break_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reconciled_orphan(monkeypatch, kind="primary")

    def _raising(*_args: object) -> None:
        raise ValueError("incomplete native tail")

    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.materialize_native_history",
        _raising,
    )

    count, ids = diag._repair_orphan_runs(tmp_path, runtime_root=tmp_path / "runtime")

    assert count == 1
    assert ids == ("p1",)
