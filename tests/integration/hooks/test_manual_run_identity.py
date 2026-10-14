from pathlib import Path

import pytest

from meridian.lib.ops.hooks import HookRunInput, hooks_run_sync


def test_hooks_run_is_noop_in_unmaterialized_adhoc_place(
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
        'command = "printf should-not-run"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())

    result = hooks_run_sync(
        HookRunInput(name="manual-test", project_root=project_root.as_posix())
    )

    assert result.result.skipped
    assert result.result.skip_reason == "no_state"
    assert not (home / "projects").exists()
    assert list(project_root.iterdir()) == []
