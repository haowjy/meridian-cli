from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.hooks.config import load_hooks_config
from meridian.lib.ops.runtime import resolve_project_authority


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "remote"
    project_root.mkdir()
    return project_root


def _empty_user_config(tmp_path: Path) -> Path:
    """Return an empty user config path for test isolation."""
    path = tmp_path / "empty-user.toml"
    path.write_text("", encoding="utf-8")
    return path


def test_load_hooks_config_parses_external_and_builtin_entries(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    user_config = tmp_path / "user.toml"
    user_config.write_text(
        "[[hooks]]\n"
        'name = "notify"\n'
        'event = "spawn.finalized"\n'
        'command = "./scripts/notify.sh"\n'
        "\n"
        "[[hooks]]\n"
        'builtin = "git-autosync"\n'
        'remote = "https://github.com/acme/project.git"\n',
        encoding="utf-8",
    )

    config = load_hooks_config(project_root, user_config=user_config)

    # notify is 1 hook, git-autosync expands to 4 hooks (one per default event)
    notify_hooks = [h for h in config.hooks if h.name == "notify"]
    autosync_hooks = [h for h in config.hooks if h.name == "git-autosync"]
    
    assert len(notify_hooks) == 1
    assert notify_hooks[0].command == "./scripts/notify.sh"
    assert notify_hooks[0].event == "spawn.finalized"
    assert notify_hooks[0].source == "user"
    
    # git-autosync expands to all 4 default events when no explicit event
    assert len(autosync_hooks) == 4
    autosync_events = {h.event for h in autosync_hooks}
    assert autosync_events == {"spawn.start", "spawn.finalized", "work.started", "work.done"}
    for h in autosync_hooks:
        assert h.builtin == "git-autosync"
        assert h.interval is None
        assert h.remote == "https://github.com/acme/project.git"


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        (
            "[[hooks]]\n"
            "name = 'bad'\n"
            "event = 'spawn.finalized'\n"
            "command = './a.sh'\n"
            "builtin = 'git-autosync'\n",
            "mutually exclusive",
        ),
        (
            "[[hooks]]\nname = 'bad'\nevent = 'spawn.finalized'\n",
            "expected either 'command' or 'builtin'",
        ),
        (
            "[[hooks]]\nname = 'bad'\nevent = 'spawn.unknown'\ncommand = './a.sh'\n",
            "expected one of",
        ),
        (
            "[[hooks]]\n"
            "name = 'bad'\n"
            "event = 'spawn.finalized'\n"
            "command = './a.sh'\n"
            "interval = 'ten'\n",
            "interval format",
        ),
        (
            "[[hooks]]\n"
            "builtin = 'missing-hook'\n",
            "expected one of",
        ),
        (
            "[[hooks]]\n"
            "builtin = 'git-autosync'\n",
            "'remote' is required for git-autosync",
        ),
    ],
)
def test_load_hooks_config_rejects_invalid_hook_definitions(
    tmp_path: Path,
    payload: str,
    expected_message: str,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=expected_message):
        load_hooks_config(project_root, user_config=_empty_user_config(tmp_path))


def test_load_hooks_config_supports_explicit_git_autosync_builtin(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[[hooks]]\n"
        'builtin = "git-autosync"\n'
        'event = "spawn.finalized"\n'
        'interval = "30m"\n'
        'remote = "https://github.com/acme/project.git"\n',
        encoding="utf-8",
    )

    config = load_hooks_config(project_root, user_config=_empty_user_config(tmp_path))

    assert len(config.hooks) == 1
    hook = config.hooks[0]
    assert hook.name == "git-autosync"
    assert hook.builtin == "git-autosync"
    assert hook.auto_registered is False
    assert hook.event == "spawn.finalized"
    assert hook.interval == "30m"
    assert hook.remote == "https://github.com/acme/project.git"


def test_load_hooks_config_does_not_auto_register_from_work_artifacts_sync(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[work.artifacts]\n"
        'sync = "git"\n',
        encoding="utf-8",
    )

    config = load_hooks_config(project_root, user_config=_empty_user_config(tmp_path))

    assert len(config.hooks) == 0


def test_load_hooks_config_applies_name_override_across_sources(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    user_config = tmp_path / "user.toml"
    user_config.write_text(
        "[[hooks]]\n"
        'name = "sync"\n'
        'event = "spawn.finalized"\n'
        'command = "./user-sync.sh"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "sync"\n'
        'event = "spawn.finalized"\n'
        'command = "./project-sync.sh"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        "[[hooks]]\n"
        'name = "sync"\n'
        'event = "spawn.finalized"\n'
        'command = "./local-sync.sh"\n',
        encoding="utf-8",
    )

    config = load_hooks_config(project_root, user_config=user_config)

    assert len(config.hooks) == 1
    assert config.hooks[0].name == "sync"
    assert config.hooks[0].source == "local"
    assert config.hooks[0].command == "./local-sync.sh"


def test_load_hooks_config_does_not_auto_register_from_context_work_source(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    user_config = tmp_path / "user.toml"
    user_config.write_text(
        "[context.work]\n"
        'source = "git"\n'
        'path = ".meridian/work"\n',
        encoding="utf-8",
    )

    config = load_hooks_config(project_root, user_config=user_config)

    assert len(config.hooks) == 0


def test_load_hooks_config_does_not_auto_register_for_kb_source_git(
    tmp_path: Path,
) -> None:
    """KB source=git does NOT auto-register git-autosync (hook only syncs work_dir)."""
    project_root = _repo(tmp_path)
    user_config = tmp_path / "user.toml"
    user_config.write_text(
        "[context.kb]\n"
        'source = "git"\n'
        'path = ".meridian/kb"\n',
        encoding="utf-8",
    )

    config = load_hooks_config(project_root, user_config=user_config)

    assert len(config.hooks) == 0


def test_load_hooks_config_explicit_builtin_works_with_context_settings_present(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    user_config = tmp_path / "user.toml"
    user_config.write_text(
        "[context.work]\n"
        'source = "git"\n'
        'path = ".meridian/work"\n'
        "\n"
        "[[hooks]]\n"
        'builtin = "git-autosync"\n'
        'event = "spawn.finalized"\n'
        'interval = "5m"\n'
        'remote = "https://github.com/acme/project.git"\n',
        encoding="utf-8",
    )

    config = load_hooks_config(project_root, user_config=user_config)

    assert len(config.hooks) == 1
    hook = config.hooks[0]
    assert hook.name == "git-autosync"
    assert hook.auto_registered is False
    assert hook.interval == "5m"
    assert hook.remote == "https://github.com/acme/project.git"


def test_load_hooks_config_prefers_shared_authority_paths(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    authority_root = tmp_path / "authority-repo"
    authority_root.mkdir()
    authority = resolve_project_authority(authority_root)
    authority.project_config_paths.meridian_toml.write_text(
        "[[hooks]]\n"
        'name = "authority-hook"\n'
        'event = "spawn.finalized"\n'
        'command = "./authority.sh"\n',
        encoding="utf-8",
    )

    config = load_hooks_config(
        project_root,
        user_config=_empty_user_config(tmp_path),
        authority=authority,
    )

    assert len(config.hooks) == 1
    assert config.hooks[0].name == "authority-hook"
    assert config.hooks[0].command == "./authority.sh"
    assert config.hooks[0].source == "project"
