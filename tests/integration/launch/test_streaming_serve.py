# qa-validated: test-suite-redesign
from __future__ import annotations

from pathlib import Path

import pytest

from meridian.cli import streaming_serve as streaming_serve_module
from meridian.lib.core.types import HarnessId
from meridian.lib.ops.runtime import resolve_runtime_root
from meridian.lib.state.spawn_store import get_spawn
from meridian.lib.streaming.spawn_manager import DrainOutcome
from tests.support.launch import stub_bundle_request_and_resolve


@pytest.fixture(autouse=True)
def _stub_launch_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
        harness_model="gpt-5.4",
    )


@pytest.mark.asyncio
async def test_streaming_serve_shutdown_finalizes_once_as_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_root(tmp_path)
    helper_calls: list[tuple[str, str]] = []

    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        helper_calls.append((str(kwargs["spawn_id"]), str(kwargs["runtime_root"])))
        return DrainOutcome(status="cancelled", exit_code=1)

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    await streaming_serve_module.streaming_serve("codex", "hello")

    assert helper_calls == [("p1", str(runtime_root))]
    row = get_spawn(runtime_root, "p1")
    assert row is not None
    assert row.status == "cancelled"
    assert row.terminal.exit_code == 1


@pytest.mark.asyncio
async def test_streaming_serve_start_failure_finalizes_failed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_root(tmp_path)

    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        _ = kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    with pytest.raises(RuntimeError, match="boom"):
        await streaming_serve_module.streaming_serve("codex", "hello")

    row = get_spawn(runtime_root, "p1")
    assert row is not None
    assert row.status == "failed"
    assert row.terminal.error == "boom"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported_endpoint",
    [
        "unix:///tmp/.meridian/spawns/p1/control.sock",
        "tcp://127.0.0.1:43125",
    ],
)
async def test_streaming_serve_reports_platform_control_endpoint(
    reported_endpoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        callback = kwargs.get("on_control_endpoint_ready")
        assert callable(callback)
        callback(reported_endpoint)
        return DrainOutcome(status="succeeded", exit_code=0)

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    await streaming_serve_module.streaming_serve("codex", "hello")

    output = capsys.readouterr().out
    assert f"Control endpoint: {reported_endpoint}" in output
