"""Dispatch refuses tracked direct specs before creating a connection."""

from pathlib import Path

import pytest

import meridian.lib.streaming.spawn_dispatch as spawn_dispatch
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import session_store
from meridian.lib.state.session_authority import (
    LocalObjectStamp,
    NativeSessionKey,
    NativeSourceRef,
    QualifiedLocalFile,
    RecordedNativeSource,
)


def _recorded_source() -> RecordedNativeSource:
    return RecordedNativeSource(
        ref=NativeSourceRef(chat_id="c1", binding_event_id="a" * 64, locator_event_id="b" * 64),
        key=NativeSessionKey(harness="pi", store="/store", native_session_id="KNOWN"),
        locator=QualifiedLocalFile(
            kind="local_file",
            path="/store/KNOWN.jsonl",
            store_object=LocalObjectStamp(device=1, inode=2),
            file_object=LocalObjectStamp(device=1, inode=3),
            rule="pi_rpc_exact_v1",
        ),
    )


@pytest.mark.parametrize("prompt", ["task", ""])
@pytest.mark.asyncio
async def test_dispatch_refuses_recorded_source_before_connection_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
) -> None:
    started: list[bool] = []

    class RecordingConnection:
        async def start(self, _config: object, _spec: object) -> None:
            started.append(True)

    monkeypatch.setattr(spawn_dispatch, "_ensure_harness_bootstrap", lambda: None)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda *_args: RecordingConnection,
    )
    config = ConnectionConfig(
        spawn_id="p1",
        harness_id=HarnessId.PI,
        prompt=prompt,
        control_root=tmp_path,
        child_env={},
        runtime_root=tmp_path / "runtime",
    )
    spec = ResolvedLaunchSpec(
        prompt=prompt,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        recorded_native_source=_recorded_source(),
    )

    with pytest.raises(ValueError, match="owner_required"):
        await spawn_dispatch.dispatch_start(config, spec)
    assert started == []


@pytest.mark.parametrize("harness", [HarnessId.PI, HarnessId.CODEX])
@pytest.mark.asyncio
async def test_dispatch_refuses_selection_matching_recorded_native_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: HarnessId,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    session_store.start_session(
        runtime_root,
        chat_id="c1",
        spawn_id="p1",
        harness=harness.value,
        harness_session_id="KNOWN",
        model="test-model",
    )
    session_store.stop_session(runtime_root, "c1")
    started: list[bool] = []

    class RecordingConnection:
        async def start(self, _config: object, _spec: object) -> None:
            started.append(True)

    monkeypatch.setattr(spawn_dispatch, "_ensure_harness_bootstrap", lambda: None)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda *_args: RecordingConnection,
    )
    config = ConnectionConfig(
        spawn_id="p1",
        harness_id=harness,
        prompt="task",
        control_root=tmp_path,
        child_env={},
        runtime_root=runtime_root,
    )
    spec = ResolvedLaunchSpec(
        prompt="task",
        continue_session_id="KNOWN",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(ValueError, match="not eligible"):
        await spawn_dispatch.dispatch_start(config, spec)
    assert started == []


@pytest.mark.parametrize(
    "extra_args",
    ["-c", "-r", "--continue", "--resume", "--continue=latest", "--resume=latest"],
)
@pytest.mark.parametrize("prompt", ["task", ""])
@pytest.mark.asyncio
async def test_dispatch_refuses_raw_pi_selection_option_before_connection_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: str,
    prompt: str,
) -> None:
    started: list[bool] = []

    class RecordingConnection:
        async def start(self, _config: object, _spec: object) -> None:
            started.append(True)

    monkeypatch.setattr(spawn_dispatch, "_ensure_harness_bootstrap", lambda: None)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda *_args: RecordingConnection,
    )
    config = ConnectionConfig(
        spawn_id="p1",
        harness_id=HarnessId.PI,
        prompt=prompt,
        control_root=tmp_path,
        child_env={},
        runtime_root=tmp_path / "runtime",
    )
    spec = ResolvedLaunchSpec(
        prompt=prompt,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        extra_args=(extra_args,),
    )
    with pytest.raises(ValueError, match="Raw Pi native selection"):
        await spawn_dispatch.dispatch_start(config, spec)
    assert started == []


@pytest.mark.parametrize("harness", [HarnessId.PI, HarnessId.CODEX])
def test_dispatch_policy_preserves_negative_native_ids_and_fresh(
    tmp_path: Path,
    harness: HarnessId,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    config = ConnectionConfig(
        spawn_id="p1",
        harness_id=harness,
        prompt="",
        control_root=tmp_path,
        child_env={},
        runtime_root=runtime_root,
    )
    permission_resolver = UnsafeNoOpPermissionResolver(_suppress_warning=True)

    spawn_dispatch._refuse_unowned_tracked_selection(
        config,
        ResolvedLaunchSpec(
            permission_resolver=permission_resolver,
            continue_session_id="not-recorded",
        ),
    )
    spawn_dispatch._refuse_unowned_tracked_selection(
        config,
        ResolvedLaunchSpec(permission_resolver=permission_resolver),
    )
