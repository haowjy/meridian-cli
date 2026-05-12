"""Integration coverage for extraction paths that require real filesystem state."""

import os
import time
from pathlib import Path

import pytest

from meridian.lib.harness.extractors.opencode import OpenCodeHarnessExtractor
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def test_opencode_extractor_falls_back_to_xdg_session_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_data_home = tmp_path / "xdg-data"
    session_diff_dir = xdg_data_home / "opencode" / "storage" / "session_diff"
    session_diff_dir.mkdir(parents=True)

    older_session_id = "ses_older_session_12345"
    fake_session_id = "ses_fake_session_67890"
    older_file = session_diff_dir / f"{older_session_id}.json"
    target_file = session_diff_dir / f"{fake_session_id}.json"
    older_file.write_text("[]", encoding="utf-8")
    target_file.write_text("[]", encoding="utf-8")
    now = time.time()
    os.utime(older_file, (now - 20, now - 20))
    os.utime(target_file, (now, now))

    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())
    extractor = OpenCodeHarnessExtractor()
    spec = ResolvedLaunchSpec(
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    empty_child_cwd = tmp_path / "child"
    empty_state_root = tmp_path / "state"
    empty_child_cwd.mkdir()
    empty_state_root.mkdir()

    detected = extractor.detect_session_id_from_artifacts(
        spec=spec,
        launch_env={"XDG_DATA_HOME": xdg_data_home.as_posix()},
        child_cwd=empty_child_cwd,
        runtime_root=empty_state_root,
    )

    assert detected == fake_session_id
