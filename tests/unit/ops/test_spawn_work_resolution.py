# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.context import RuntimeContext
from meridian.lib.ops.spawn.execute import _resolve_work_id
from meridian.lib.ops.spawn.models import SpawnCreateInput

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_resolve_work_id_prefers_payload_work() -> None:
    payload = SpawnCreateInput(work="payload-work")
    resolved = _resolve_work_id(
        payload=payload,
        runtime_context=RuntimeContext(work_id="runtime-work"),
        runtime_root=Path("/runtime/state"),
    )

    assert resolved == "payload-work"


def test_resolve_work_id_falls_back_to_runtime_context_work() -> None:
    payload = SpawnCreateInput(work="")
    resolved = _resolve_work_id(
        payload=payload,
        runtime_context=RuntimeContext(work_id="runtime-work"),
        runtime_root=Path("/runtime/state"),
    )

    assert resolved == "runtime-work"


def test_resolve_work_id_uses_persisted_current_work(monkeypatch: MonkeyPatch) -> None:
    payload = SpawnCreateInput(work="")

    def fake_get_current_work_id(runtime_root: Path) -> str | None:
        return "persisted-work" if runtime_root == Path("/runtime/state") else None

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.execute.get_current_work_id",
        fake_get_current_work_id,
    )

    resolved = _resolve_work_id(
        payload=payload,
        runtime_context=RuntimeContext(),
        runtime_root=Path("/runtime/state"),
    )

    assert resolved == "persisted-work"
