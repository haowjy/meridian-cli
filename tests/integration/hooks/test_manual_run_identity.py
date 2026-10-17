from pathlib import Path

import pytest

from meridian.lib.ops.hooks import HookRunInput, hooks_run_sync
from meridian.server.main import extension_invoke


def test_hooks_run_creates_no_state_in_unmaterialized_adhoc_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "task-dir"
    project_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        "[[hooks]]\n"
        'name = "manual-test"\n'
        'event = "spawn.finalized"\n'
        'command = "true"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())

    result = hooks_run_sync(
        HookRunInput(name="manual-test", project_root=project_root.as_posix())
    )

    assert result.result.success
    assert "identity_created" not in result.model_dump()
    assert not (home / "projects").exists()
    assert list(project_root.iterdir()) == []


@pytest.mark.asyncio
async def test_mcp_hooks_run_is_read_only_and_omits_identity_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "task-dir"
    project_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        "[[hooks]]\n"
        'name = "manual-test"\n'
        'event = "spawn.finalized"\n'
        'command = "true"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())
    monkeypatch.chdir(project_root)

    response = await extension_invoke(
        "meridian.hooks.run",
        {"name": "manual-test"},
    )

    assert response["status"] == "ok"
    result = response["result"]
    assert result["result"]["success"]
    assert "identity_created" not in result
    assert not (home / "projects").exists()
