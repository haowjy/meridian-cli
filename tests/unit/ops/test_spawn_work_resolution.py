# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.context import RuntimeContext
from meridian.lib.ops.spawn.execute import _resolve_work_id
from meridian.lib.ops.spawn.models import SpawnCreateInput


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


def test_resolve_work_id_returns_none_when_no_work_context() -> None:
    payload = SpawnCreateInput(work="")
    resolved = _resolve_work_id(
        payload=payload,
        runtime_context=RuntimeContext(),
        runtime_root=Path("/runtime/state"),
    )

    assert resolved is None
