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


def test_config_set_requires_project_config_file(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)

    with pytest.raises(ValueError, match="no project config; run `meridian config init`"):
        config_set_sync(
            ConfigSetInput(
                project_root=project_root.as_posix(),
                key="defaults.max_depth",
                value="5",
            )
        )


@pytest.mark.parametrize(
    "key",
    ["defaults.model"],
)
@pytest.mark.parametrize("operation", ["get", "set"])
def test_deleted_routing_default_keys_are_not_supported_by_config_commands(
    tmp_path: Path,
    key: str,
    operation: str,
) -> None:
    project_root = _repo(tmp_path)
    config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))

    with pytest.raises(ValueError, match=rf"Unknown config key '{key}'"):
        if operation == "get":
            config_get_sync(ConfigGetInput(project_root=project_root.as_posix(), key=key))
        elif operation == "set":
            config_set_sync(
                ConfigSetInput(project_root=project_root.as_posix(), key=key, value="codex")
            )
        else:
            config_reset_sync(ConfigResetInput(project_root=project_root.as_posix(), key=key))


@pytest.mark.parametrize(
    (
        "project_toml",
        "local_toml",
        "user_toml",
        "env",
        "key",
        "expected_value",
        "expected_source",
        "expected_env_var",
    ),
    [
        (
            '[primary]\nagent = "reviewer"\n',
            None,
            '[primary]\nagent = "coder"\n',
            {},
            "primary.agent",
            "reviewer",
            "file",
            None,
        ),
        (
            '[primary]\nagent = "reviewer"\n',
            None,
            '[primary]\nagent = "coder"\n',
            {"MERIDIAN_AGENT": "planner"},
            "primary.agent",
            "planner",
            "env var",
            "MERIDIAN_AGENT",
        ),
        (
            None,
            None,
            '[primary]\nagent = "reviewer"\n',
            {},
            "primary.agent",
            "reviewer",
            "user-config",
            None,
        ),
        (
            '[primary]\nagent = "reviewer"\n',
            '[primary]\nagent = "coder"\n',
            None,
            {},
            "primary.agent",
            "coder",
            "file",
            None,
        ),
        (
            None,
            None,
            None,
            {
                "MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS": "120",
                "MERIDIAN_MIN_WAIT_YIELD_SECONDS": "45",
            },
            "spawn.default_wait_yield_seconds",
            120.0,
            "env var",
            "MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS",
        ),
        (
            None,
            None,
            None,
            {
                "MERIDIAN_DEFAULT_WAIT_YIELD_SECONDS": "120",
                "MERIDIAN_MIN_WAIT_YIELD_SECONDS": "45",
            },
            "spawn.min_wait_yield_seconds",
            45.0,
            "env var",
            "MERIDIAN_MIN_WAIT_YIELD_SECONDS",
        ),
    ],
    ids=[
        "project-over-user",
        "env-over-project",
        "env-selected-user-config",
        "local-over-project",
        "default-wait-env",
        "min-wait-env",
    ],
)
def test_config_show_get_and_loader_report_same_winning_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_toml: str | None,
    local_toml: str | None,
    user_toml: str | None,
    env: dict[str, str],
    key: str,
    expected_value: object,
    expected_source: str,
    expected_env_var: str | None,
) -> None:
    project_root = _repo(tmp_path)
    if project_toml is not None:
        (project_root / "meridian.toml").write_text(project_toml, encoding="utf-8")
    if local_toml is not None:
        (project_root / "meridian.local.toml").write_text(local_toml, encoding="utf-8")
    if user_toml is not None:
        user_config = tmp_path / "user-config.toml"
        user_config.write_text(user_toml, encoding="utf-8")
        monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())
    for env_var, value in env.items():
        monkeypatch.setenv(env_var, value)

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    gotten = config_get_sync(ConfigGetInput(project_root=project_root.as_posix(), key=key))
    shown_value = next(item for item in shown.values if item.key == key)

    assert shown_value.value == expected_value
    assert shown_value.source == expected_source
    assert shown_value.env_var == expected_env_var
    assert gotten.key == key
    assert gotten.value == expected_value
    assert gotten.source == expected_source
    assert gotten.env_var == expected_env_var
    loaded = load_config(project_root)
    if key == "primary.agent":
        assert loaded.primary.agent == expected_value


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
        "[primary]\n"
        'agent = "reviewer" # inline comment\n'
        "\n"
        "[context.work]\n"
        'source = "git"\n'
        'remote = "https://example.com/work.git"\n'
        "\n"
        "[workspace.docs]\n"
        'path = "./docs"\n'
        "\n"
        "[custom_agents.reviewer]\n"
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
            key="primary.agent",
            value="coder",
        )
    )

    updated = config_path.read_text(encoding="utf-8")

    assert result.key == "primary.agent"
    assert result.value == "coder"
    assert "# top-level comment" in updated
    assert 'agent = "coder" # inline comment' in updated
    assert "[context.work]" in updated
    assert 'remote = "https://example.com/work.git"' in updated
    assert "[workspace.docs]" in updated
    assert "[custom_agents.reviewer]" in updated
    assert 'model = "gpt55"' in updated
    assert "[custom]" in updated
    assert 'value = "keep-me"' in updated
    assert "[[hooks]]" in updated
    assert 'run = "echo hi"' in updated
    assert load_config(project_root).primary.agent == "coder"


def test_config_set_spawn_deny_headless_harnesses_empty_list_round_trip(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))

    set_result = config_set_sync(
        ConfigSetInput(
            project_root=project_root.as_posix(),
            key="spawn.deny_headless_harnesses",
            value="[]",
        )
    )
    gotten = config_get_sync(
        ConfigGetInput(
            project_root=project_root.as_posix(),
            key="spawn.deny_headless_harnesses",
        )
    )

    assert set_result.key == "spawn.deny_headless_harnesses"
    assert set_result.value == ()
    assert gotten.value == ()
    assert gotten.source == "file"
    assert load_config(project_root).deny_headless_harnesses == ()


def test_config_set_rewrites_nested_harness_alias_to_canonical_spelling(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    config_path = project_root / "meridian.toml"
    config_path.write_text(
        '[harness.codex]\nmodel = "gpt-5.4"\n\n[custom]\nvalue = "keep-me"\n',
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
