from pathlib import Path

import pytest

from meridian.lib.hooks.builtin import BUILTIN_HOOKS
from meridian.lib.ops.hooks import HookRunInput, hooks_run_sync
from meridian.plugin_api import HookContext, HookResult
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
        '[[hooks]]\nname = "manual-test"\nevent = "spawn.finished"\ncommand = "true"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_HOME", home.as_posix())

    result = hooks_run_sync(HookRunInput(name="manual-test", project_root=project_root.as_posix()))

    assert result.result.success
    assert "identity_created" not in result.model_dump()
    assert not (home / "projects").exists()
    assert list(project_root.iterdir()) == []


def test_hooks_run_rejects_notify_builtin_without_delivering(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"meridian notify"):
        hooks_run_sync(
            HookRunInput(
                name="notify",
                event="spawn.finished",
                project_root=tmp_path.as_posix(),
            )
        )


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
        '[[hooks]]\nname = "manual-test"\nevent = "spawn.finished"\ncommand = "true"\n',
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


def test_hooks_run_uses_one_same_spec_registration_but_requires_event_for_different_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "task-dir"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "same-spec"\n'
        'builtin = "git-autosync"\n'
        'remote = "https://example.com/notes.git"\n',
        encoding="utf-8",
    )
    calls: list[str] = []

    def execute(context: HookContext, _config: object) -> HookResult:
        calls.append(context.event_name)
        return HookResult(
            hook_name="same-spec",
            event=context.event_name,
            outcome="success",
            success=True,
        )

    monkeypatch.setattr(BUILTIN_HOOKS["git-autosync"], "execute", execute)

    result = hooks_run_sync(HookRunInput(name="same-spec", project_root=project_root.as_posix()))

    assert result.result.success
    assert calls == ["work.done"]

    overridden = hooks_run_sync(
        HookRunInput(
            name="same-spec",
            event="spawn.created",
            project_root=project_root.as_posix(),
        )
    )

    assert overridden.result.success
    assert calls == ["work.done", "spawn.created"]

    (project_root / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "different"\n'
        'event = "spawn.started"\n'
        'command = "true"\n'
        "[[hooks]]\n"
        'name = "different"\n'
        'event = "work.done"\n'
        'command = "false"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="pass --event"):
        hooks_run_sync(HookRunInput(name="different", project_root=project_root.as_posix()))
