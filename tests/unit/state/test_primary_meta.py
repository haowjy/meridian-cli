# qa-validated: pi-rpc-quiescence
"""Primary metadata schema tests."""

from __future__ import annotations

from meridian.lib.state.primary_meta import (
    PrimaryMetadata,
    is_managed_primary,
    read_primary_harness_session_discovery,
    read_primary_harness_session_id,
    read_primary_metadata,
    read_primary_surface_metadata,
    write_primary_metadata,
)


def test_primary_surface_and_session_id_projection_support_native_metadata(tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_dir = runtime_root / "spawns" / "p2"
    spawn_dir.mkdir(parents=True)

    write_primary_metadata(
        spawn_dir,
        PrimaryMetadata(
            managed_backend=False,
            launcher_pid=111,
            tui_pid=222,
            activity="finalizing",
            harness_session_id="ses-native",
            harness_session_discovery="discovery_failed",
            harness_session_discovery_detail="no_matching_session: cwd=/tmp/repo after=100.0",
        ),
    )

    surface = read_primary_surface_metadata(runtime_root, "p2")

    assert surface.managed_backend is False
    assert surface.activity == "finalizing"
    assert surface.tui_pid == 222
    assert surface.harness_session_id == "ses-native"
    assert surface.harness_session_discovery == "discovery_failed"
    assert (
        surface.harness_session_discovery_detail
        == "no_matching_session: cwd=/tmp/repo after=100.0"
    )
    assert read_primary_harness_session_id(runtime_root, "p2") == "ses-native"
    assert read_primary_harness_session_discovery(runtime_root, "p2") == (
        "discovery_failed",
        "no_matching_session: cwd=/tmp/repo after=100.0",
    )
    assert is_managed_primary(runtime_root, "p2") is False


def test_read_primary_metadata_preserves_native_wrapper_runtime_fields(tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    spawn_dir = runtime_root / "spawns" / "p3"
    spawn_dir.mkdir(parents=True)

    write_primary_metadata(
        spawn_dir,
        PrimaryMetadata(
            managed_backend=False,
            launcher_pid=333,
            tui_pid=444,
            activity="finalizing",
            harness_session_id="ses-native",
            command=("/usr/local/bin/pi", "--model", "openai-codex/gpt-5.4-mini"),
            launch_cwd="/tmp/project-root",
            started_at_epoch=100.5,
            ended_at_epoch=112.25,
            exit_code=7,
            runtime_kind="path",
            runtime_path="/usr/local/bin/pi",
            runtime_version="pi 4.5.6",
            session_dir="/tmp/sessions",
            auth_policy="inherit-runtime-default-auth-config",
        ),
    )

    metadata = read_primary_metadata(runtime_root, "p3")
    assert metadata is not None
    assert metadata.command == ("/usr/local/bin/pi", "--model", "openai-codex/gpt-5.4-mini")
    assert metadata.launch_cwd == "/tmp/project-root"
    assert metadata.started_at_epoch == 100.5
    assert metadata.ended_at_epoch == 112.25
    assert metadata.exit_code == 7
    assert metadata.runtime_kind == "path"
    assert metadata.runtime_path == "/usr/local/bin/pi"
    assert metadata.runtime_version == "pi 4.5.6"
    assert metadata.session_dir == "/tmp/sessions"
    assert metadata.auth_policy == "inherit-runtime-default-auth-config"
