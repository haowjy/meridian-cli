"""Exact tracked Pi resume projects only its recorded, preflighted source."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.harness.projections.project_pi_native_tui import (
    project_pi_native_tui_spec_to_cli_args,
)
from meridian.lib.harness.projections.project_pi_rpc import project_pi_spec_to_cli_args
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver
from meridian.lib.state.session_authority import (
    LocalObjectStamp,
    NativeSessionKey,
    NativeSourceRef,
    QualifiedLocalFile,
    RecordedNativeSource,
)


def _source(store: Path, file: Path, session_id: str = "A") -> RecordedNativeSource:
    store_stat = store.stat()
    file_stat = file.stat()
    return RecordedNativeSource(
        ref=NativeSourceRef(
            chat_id="c1", binding_event_id="a" * 64, locator_event_id="b" * 64
        ),
        key=NativeSessionKey(
            harness="pi", store=str(store), native_session_id=session_id
        ),
        locator=QualifiedLocalFile(
            kind="local_file",
            path=str(file),
            store_object=LocalObjectStamp(device=store_stat.st_dev, inode=store_stat.st_ino),
            file_object=LocalObjectStamp(device=file_stat.st_dev, inode=file_stat.st_ino),
            rule="pi_rpc_exact_v1",
        ),
    )


def _spec(source: RecordedNativeSource, *, extra_args: tuple[str, ...] = ()) -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        harness="pi",
        continue_session_id=source.key.native_session_id,
        recorded_native_source=source,
        permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
        extra_args=extra_args,
    )


def test_tracked_resume_projects_exact_file_and_pinned_store(tmp_path: Path) -> None:
    store = tmp_path / "native-store"
    store.mkdir()
    file = store / "nested" / "picked.jsonl"
    file.parent.mkdir()
    file.write_text(json.dumps({"type": "session", "version": 3, "id": "A"}) + "\n")
    source = _source(store, file)

    argv = project_pi_spec_to_cli_args(_spec(source), base_command=("pi",))

    assert argv[argv.index("--session") + 1] == str(file)
    assert argv[argv.index("--session-dir") + 1] == str(store)


def test_native_tui_projector_refuses_recorded_source(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    file = store / "session.jsonl"
    file.write_text(json.dumps({"type": "session", "version": 3, "id": "A"}) + "\n")
    source = _source(store, file)

    with pytest.raises(ValueError, match="transport_unqualified"):
        project_pi_native_tui_spec_to_cli_args(_spec(source), base_command=("pi",))


@pytest.mark.parametrize("argument", [
    "task text", "@instructions.txt", "--session=/tmp/other", "--append-system-prompt=x",
    "--mode=rpc", "--extension=x", "--reload", "--unknown-flag", "--session-dir",
    "--session-dir=/tmp/override",
])
def test_tracked_resume_rejects_raw_passthrough(tmp_path: Path, argument: str) -> None:
    store = tmp_path / "store"
    store.mkdir()
    file = store / "session.jsonl"
    file.write_text(json.dumps({"type": "session", "version": 3, "id": "A"}) + "\n")
    source = _source(store, file)

    with pytest.raises(ValueError, match="raw extra_args"):
        project_pi_spec_to_cli_args(_spec(source, extra_args=(argument,)), base_command=("pi",))


def test_tracked_resume_refuses_changed_header_before_projection(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    file = store / "session.jsonl"
    file.write_text(json.dumps({"type": "session", "version": 3, "id": "A"}) + "\n")
    source = _source(store, file)
    file.write_text(json.dumps({"type": "session", "version": 3, "id": "B"}) + "\n")

    with pytest.raises(ValueError, match="identity_mismatch"):
        project_pi_spec_to_cli_args(_spec(source), base_command=("pi",))
