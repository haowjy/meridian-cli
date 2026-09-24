"""pN replay cannot borrow a top-level cN native-source selection."""

from __future__ import annotations

from types import SimpleNamespace

from meridian.lib.core.types import HarnessId
from meridian.lib.launch.request import SessionRequest, SpawnRequest
from meridian.lib.ops.spawn.execute_session import _resolve_session_continuation
from meridian.lib.state.session_authority import (
    LocalObjectStamp,
    NativeSessionKey,
    NativeSourceRef,
    QualifiedLocalFile,
    RecordedNativeSource,
)


def test_spawn_continue_discards_recorded_primary_native_source() -> None:
    source = RecordedNativeSource(
        ref=NativeSourceRef(
            chat_id="c1", binding_event_id="a" * 64, locator_event_id="b" * 64
        ),
        key=NativeSessionKey(harness="pi", store="/store", native_session_id="A"),
        locator=QualifiedLocalFile(
            kind="local_file",
            path="/store/A.jsonl",
            store_object=LocalObjectStamp(device=1, inode=2),
            file_object=LocalObjectStamp(device=1, inode=3),
            rule="pi_rpc_exact_v1",
        ),
    )
    request = SpawnRequest(
        prompt="child task",
        session=SessionRequest(
            requested_harness_session_id="A",
            continue_source_tracked=True,
            continue_source_ref="c1",
            recorded_native_source=source,
        ),
    )
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            supports_session_resume=True, supports_session_fork=True
        )
    )

    result = _resolve_session_continuation(
        request=request, harness_id=HarnessId.PI, harness_adapter=adapter
    )

    assert result.requested_harness_session_id == "A"
    assert result.recorded_native_source is None
