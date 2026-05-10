# qa-validated: test-suite-redesign
"""Config get/set/reset operations and precedence resolution tests."""

from pathlib import Path

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.ops.config import (
    ConfigGetInput,
    ConfigInitInput,
    ConfigResetInput,
    ConfigSetInput,
    ConfigShowInput,
    config_get_sync,
    config_init_sync,
    config_reset_sync,
    config_set_sync,
    config_show_sync,
)


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


@pytest.mark.parametrize("operation", ["set", "reset"])
def test_config_set_and_reset_require_project_config_file(
    tmp_path: Path,
    operation: str,
) -> None:
    project_root = _repo(tmp_path)

    with pytest.raises(ValueError, match="no project config; run `meridian config init`"):
        if operation == "set":
            config_set_sync(
                ConfigSetInput(
                    project_root=project_root.as_posix(),
                    key="defaults.model",
                    value="gpt-5.4",
                )
            )
        else:
            config_reset_sync(
                ConfigResetInput(
                    project_root=project_root.as_posix(),
                    key="defaults.model",
                )
            )


def test_config_show_and_loader_share_project_config_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    project_config = project_root / "meridian.toml"
    project_config.write_text('[defaults]\nharness = "claude"\n', encoding="utf-8")
    user_config = tmp_path / "user-config.toml"
    user_config.write_text('[defaults]\nharness = "opencode"\n', encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())

    project_only = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    project_only_value = next(
        item for item in project_only.values if item.key == "defaults.harness"
    )
    assert project_only.path == project_config.as_posix()
    assert project_only_value.value == "claude"
    assert project_only_value.source == "file"
    assert load_config(project_root).default_harness == "claude"

    monkeypatch.setenv("MERIDIAN_DEFAULT_HARNESS", "codex")

    resolved = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    resolved_value = next(item for item in resolved.values if item.key == "defaults.harness")

    assert resolved.path == project_config.as_posix()
    assert resolved_value.value == "codex"
    assert resolved_value.source == "env var"
    assert resolved_value.env_var == "MERIDIAN_DEFAULT_HARNESS"
    assert load_config(project_root).default_harness == "codex"


def test_config_show_and_get_resolve_env_selected_user_config_like_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    env_user_config = tmp_path / "env-user-config.toml"
    env_user_config.write_text('[defaults]\nharness = "opencode"\n', encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_CONFIG", env_user_config.as_posix())

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    gotten = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="defaults.harness")
    )
    shown_value = next(item for item in shown.values if item.key == "defaults.harness")

    assert shown_value.value == "opencode"
    assert shown_value.source == "user-config"
    assert gotten.key == "defaults.harness"
    assert gotten.value == "opencode"
    assert gotten.source == "user-config"
    assert load_config(project_root).default_harness == "opencode"


def test_config_show_and_loader_share_local_over_project_precedence(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        '[defaults]\nharness = "claude"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        '[defaults]\nharness = "opencode"\n',
        encoding="utf-8",
    )

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    shown_value = next(item for item in shown.values if item.key == "defaults.harness")
    gotten = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="defaults.harness")
    )

    assert shown_value.value == "opencode"
    assert shown_value.source == "file"
    assert gotten.value == "opencode"
    assert gotten.source == "file"
    assert load_config(project_root).default_harness == "opencode"


def test_config_show_and_get_report_spawn_wait_yield_env_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    monkeypatch.setenv("MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS", "120")
    monkeypatch.setenv("MERIDIAN_MIN_WAIT_YIELD_SECONDS", "45")

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    shown_default = next(
        item for item in shown.values if item.key == "spawn.default_wait_yield_seconds"
    )
    shown_min = next(item for item in shown.values if item.key == "spawn.min_wait_yield_seconds")
    gotten_default = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="spawn.default_wait_yield_seconds")
    )
    gotten_min = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="spawn.min_wait_yield_seconds")
    )

    assert shown_default.value == 120.0
    assert shown_default.source == "env var"
    assert shown_default.env_var == "MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS"
    assert shown_min.value == 45.0
    assert shown_min.source == "env var"
    assert shown_min.env_var == "MERIDIAN_MIN_WAIT_YIELD_SECONDS"
    assert gotten_default.value == 120.0
    assert gotten_default.source == "env var"
    assert gotten_default.env_var == "MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS"
    assert gotten_min.value == 45.0
    assert gotten_min.source == "env var"
    assert gotten_min.env_var == "MERIDIAN_MIN_WAIT_YIELD_SECONDS"


def test_config_state_retention_days_round_trip(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    init = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))
    project_config = project_root / "meridian.toml"
    assert init.created is True
    assert "[state]" in project_config.read_text(encoding="utf-8")

    set_result = config_set_sync(
        ConfigSetInput(
            project_root=project_root.as_posix(),
            key="state.retention_days",
            value="14",
        )
    )
    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    gotten = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="state.retention_days")
    )
    retention = next(item for item in shown.values if item.key == "state.retention_days")

    assert set_result.key == "state.retention_days"
    assert set_result.value == 14
    assert retention.value == 14
    assert retention.source == "file"
    assert gotten.value == 14
    assert gotten.source == "file"
    assert load_config(project_root).state.retention_days == 14

    reset_result = config_reset_sync(
        ConfigResetInput(project_root=project_root.as_posix(), key="state.retention_days")
    )

    assert reset_result.removed is True
    assert load_config(project_root).state.retention_days == 30


def test_config_set_preserves_dynamic_sections_comments_and_unknown_content(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    config_path = project_root / "meridian.toml"
    original = (
        "# top-level comment\n"
        "[defaults]\n"
        'harness = "claude" # inline comment\n'
        "\n"
        "[context.work]\n"
        'source = "git"\n'
        'remote = "https://example.com/work.git"\n'
        "\n"
        "[workspace.docs]\n"
        'path = "./docs"\n'
        "\n"
        "[agents.reviewer]\n"
        'model = "gpt55"\n'
        "\n"
        "[custom]\n"
        'value = "keep-me"\n'
        "\n"
        "[[hooks]]\n"
        'event = "spawn"\n'
        'run = "echo hi"\n'
    )
    config_path.write_text(original, encoding="utf-8")

    result = config_set_sync(
        ConfigSetInput(
            project_root=project_root.as_posix(),
            key="defaults.harness",
            value="opencode",
        )
    )

    updated = config_path.read_text(encoding="utf-8")

    assert result.key == "defaults.harness"
    assert result.value == "opencode"
    assert '# top-level comment' in updated
    assert 'harness = "opencode" # inline comment' in updated
    assert "[context.work]" in updated
    assert 'remote = "https://example.com/work.git"' in updated
    assert "[workspace.docs]" in updated
    assert '[agents.reviewer]' in updated
    assert 'model = "gpt55"' in updated
    assert "[custom]" in updated
    assert 'value = "keep-me"' in updated
    assert "[[hooks]]" in updated
    assert 'run = "echo hi"' in updated
    assert load_config(project_root).default_harness == "opencode"


def test_config_reset_preserves_dynamic_sections_comments_and_unknown_content(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    config_path = project_root / "meridian.toml"
    config_path.write_text(
        "# top-level comment\n"
        "[defaults]\n"
        'harness = "claude" # inline comment\n'
        "\n"
        "[context.work]\n"
        'source = "git"\n'
        'remote = "https://example.com/work.git"\n'
        "\n"
        "[workspace.docs]\n"
        'path = "./docs"\n'
        "\n"
        "[custom]\n"
        'value = "keep-me"\n'
        "\n"
        "[[hooks]]\n"
        'event = "spawn"\n'
        'run = "echo hi"\n',
        encoding="utf-8",
    )

    result = config_reset_sync(
        ConfigResetInput(project_root=project_root.as_posix(), key="defaults.harness")
    )

    updated = config_path.read_text(encoding="utf-8")

    assert result.removed is True
    assert "# top-level comment" in updated
    assert 'harness = "claude"' not in updated
    assert "[context.work]" in updated
    assert "[workspace.docs]" in updated
    assert "[custom]" in updated
    assert "[[hooks]]" in updated
    assert 'run = "echo hi"' in updated
    assert load_config(project_root).default_harness == "codex"


def test_config_set_rewrites_nested_harness_alias_to_canonical_spelling(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    config_path = project_root / "meridian.toml"
    config_path.write_text(
        "[harness.codex]\n"
        'model = "gpt-5.4"\n'
        "\n"
        "[custom]\n"
        'value = "keep-me"\n',
        encoding="utf-8",
    )

    result = config_set_sync(
        ConfigSetInput(project_root=project_root.as_posix(), key="harness.codex", value="gpt-5.5")
    )

    updated = config_path.read_text(encoding="utf-8")

    assert result.key == "harness.codex"
    assert result.value == "gpt-5.5"
    assert "[harness]" in updated
    assert 'codex = "gpt-5.5"' in updated
    assert "[harness.codex]" not in updated
    assert "[custom]" in updated
    assert load_config(project_root).harness.codex.model == "gpt-5.5"
