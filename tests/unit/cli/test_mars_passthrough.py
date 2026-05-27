from __future__ import annotations

from pathlib import Path

import pytest

from meridian.cli import mars_passthrough


@pytest.fixture(autouse=True)
def _clear_meridian_project_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)


def test_parse_injects_root_from_meridian_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(project_root))

    request = mars_passthrough.parse_mars_passthrough(
        ["models", "list"],
        executable="/usr/bin/mars",
    )

    assert request.command == (
        "/usr/bin/mars",
        "models",
        "list",
        "--root",
        str(project_root),
    )
    assert request.root_override == project_root
    assert request.is_sync is False
    assert request.wants_json is False


def test_parse_omits_root_when_meridian_project_dir_unset() -> None:
    request = mars_passthrough.parse_mars_passthrough(
        ["models", "list"],
        executable="/usr/bin/mars",
    )

    assert request.command == ("/usr/bin/mars", "models", "list")
    assert request.root_override is None


def test_parse_respects_user_root_over_meridian_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launcher_root = tmp_path / "launcher"
    user_root = tmp_path / "user"
    launcher_root.mkdir()
    user_root.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(launcher_root))

    request = mars_passthrough.parse_mars_passthrough(
        ["--root", str(user_root), "models", "list"],
        executable="/usr/bin/mars",
    )

    assert request.command == (
        "/usr/bin/mars",
        "--root",
        str(user_root),
        "models",
        "list",
    )
    assert request.root_override == user_root
    assert list(request.command).count("--root") == 1


def test_parse_json_and_sync_with_meridian_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(project_root))

    sync_request = mars_passthrough.parse_mars_passthrough(
        ["sync"],
        executable="/usr/bin/mars",
    )
    assert sync_request.is_sync is True
    assert sync_request.wants_json is False
    assert sync_request.command[-2:] == ("--root", str(project_root))

    json_request = mars_passthrough.parse_mars_passthrough(
        ["models", "resolve", "haiku"],
        executable="/usr/bin/mars",
        output_format="json",
    )
    assert json_request.wants_json is True
    assert json_request.command[:2] == ("/usr/bin/mars", "--json")
    assert json_request.command[-2:] == ("--root", str(project_root))
