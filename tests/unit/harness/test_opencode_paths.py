"""Tests for centralized OpenCode data path resolution."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.harness import opencode_storage
from meridian.lib.harness.opencode_storage import (
    resolve_opencode_home_dir,
    resolve_opencode_storage_root,
)
from meridian.lib.harness.opencode_transcript import resolve_opencode_db_path


def test_resolve_opencode_storage_root_uses_localappdata_on_windows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_app_data = tmp_path / "localappdata"
    local_app_data.mkdir()

    monkeypatch.setattr(opencode_storage, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_HOME", raising=False)
    # Native Windows with no explicit HOME override: LOCALAPPDATA is the default.
    # (An explicitly-set HOME/XDG wins over it — covered by other tests.)
    monkeypatch.delenv("HOME", raising=False)

    expected_storage = local_app_data / "opencode" / "storage"
    expected_db = local_app_data / "opencode" / "opencode.db"

    assert resolve_opencode_storage_root() == expected_storage
    assert resolve_opencode_home_dir() == expected_storage.parent
    assert resolve_opencode_db_path() == expected_db


def test_resolve_opencode_storage_root_prefers_xdg_data_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(opencode_storage, "IS_WINDOWS", True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "ignored-localappdata"))
    monkeypatch.delenv("OPENCODE_HOME", raising=False)

    assert resolve_opencode_storage_root() == tmp_path / "opencode" / "storage"


def test_resolve_opencode_storage_root_uses_launch_env_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    child_home = tmp_path / "opencode-child-home"
    child_home.mkdir()

    monkeypatch.setattr(opencode_storage, "IS_WINDOWS", False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_HOME", raising=False)

    launch_env = {"HOME": str(child_home)}
    expected_storage = child_home / ".local" / "share" / "opencode" / "storage"

    assert resolve_opencode_storage_root(launch_env) == expected_storage
    assert resolve_opencode_home_dir(launch_env) == expected_storage.parent
    assert resolve_opencode_db_path(launch_env) == expected_storage.parent / "opencode.db"


def test_resolve_opencode_storage_root_explicit_home_beats_localappdata_on_windows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Regression for the Windows gate: an explicitly-set HOME must win over an
    # ambient LOCALAPPDATA (the CI runner always has LOCALAPPDATA set). OpenCode
    # honours HOME, so a spawn with HOME override must resolve under it.
    child_home = tmp_path / "home"
    local_app_data = tmp_path / "localappdata"

    monkeypatch.setattr(opencode_storage, "IS_WINDOWS", True)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("HOME", str(child_home))

    assert (
        resolve_opencode_storage_root()
        == child_home / ".local" / "share" / "opencode" / "storage"
    )


def test_resolve_opencode_storage_root_uses_launch_env_localappdata_on_windows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_app_data = tmp_path / "child-localappdata"
    local_app_data.mkdir()

    monkeypatch.setattr(opencode_storage, "IS_WINDOWS", True)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    launch_env = {"LOCALAPPDATA": str(local_app_data)}
    expected_storage = local_app_data / "opencode" / "storage"

    assert resolve_opencode_storage_root(launch_env) == expected_storage
