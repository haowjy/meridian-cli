from pathlib import Path

import pytest

from meridian.lib.catalog import models as catalog_models
from meridian.lib.config.settings import load_config
from meridian.lib.core.util import to_jsonable
from meridian.lib.ops.config import (
    ConfigGetInput,
    ConfigResetInput,
    ConfigSetInput,
    ConfigShowInput,
    config_get_sync,
    config_reset_sync,
    config_set_sync,
    config_show_sync,
)


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    monkeypatch.delenv("MERIDIAN_DEFAULT_HARNESS", raising=False)
    monkeypatch.delenv("MERIDIAN_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def test_config_show_reports_workspace_summary_when_workspace_is_absent(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    assert result.workspace.status == "none"
    assert result.workspace.sources == ()
    assert result.workspace.roots.count == 0
    assert result.workspace.roots.projected == 0
    assert result.workspace.roots.skipped == 0
    assert result.workspace.applicability == {
        "claude": "ignored:no_roots",
        "codex": "ignored:no_roots",
        "opencode": "ignored:no_roots",
    }
    assert result.workspace_findings == ()
    text = result.format_text()
    assert "workspace.status = none" in text
    assert "workspace.sources = []" in text
    assert "workspace.roots.count = 0" in text
    assert "workspace.roots.projected = 0" in text
    assert "workspace.roots.skipped = 0" in text
    assert "workspace.applicability.claude = ignored:no_roots" in text
    assert "workspace.applicability.codex = ignored:no_roots" in text
    assert "workspace.applicability.opencode = ignored:no_roots" in text


def test_config_show_does_not_create_project_config_file(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    assert result.path == (project_root / "meridian.toml").as_posix()
    assert not (project_root / "meridian.toml").exists()


def test_config_get_does_not_create_project_config_file(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="defaults.harness")
    )

    assert result.key == "defaults.harness"
    assert result.value == "codex"
    assert not (project_root / "meridian.toml").exists()


def test_config_show_json_workspace_includes_empty_findings(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    payload = to_jsonable(result)

    assert payload["workspace_findings"] == []
    workspace = payload["workspace"]
    assert workspace["sources"] == []
    assert workspace["status"] == "none"
    assert workspace["roots"] == {"count": 0, "projected": 0, "skipped": 0}
    assert workspace["applicability"] == {
        "claude": "ignored:no_roots",
        "codex": "ignored:no_roots",
        "opencode": "ignored:no_roots",
    }


def test_config_show_json_workspace_includes_path_when_present(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    workspace_path = project_root / "meridian.local.toml"
    workspace_path.write_text(
        "[workspace.missing]\npath = \"./missing-root\"\n",
        encoding="utf-8",
    )

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    payload = to_jsonable(result)

    workspace = payload["workspace"]
    assert workspace["sources"] == [workspace_path.resolve().as_posix()]
    assert workspace["roots"] == {"count": 1, "projected": 0, "skipped": 1}
    assert workspace["roots_detail"] == [
        {
            "name": "missing",
            "source": "local",
            "declared_path": "./missing-root",
            "resolved_path": (project_root / "missing-root").resolve().as_posix(),
            "status": "skipped",
        }
    ]
    assert workspace["applicability"] == {
        "claude": "ignored:no_roots",
        "codex": "ignored:no_roots",
        "opencode": "ignored:no_roots",
    }


def test_config_show_skips_missing_committed_roots(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[workspace.missing]\n"
        'path = "./missing-root"\n',
        encoding="utf-8",
    )

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    payload = to_jsonable(result)

    assert result.workspace.roots.projected == 0
    assert result.workspace.roots.skipped == 1
    assert payload["workspace"]["roots_detail"][0]["status"] == "skipped"
    assert result.workspace.applicability == {
        "claude": "ignored:no_roots",
        "codex": "ignored:no_roots",
        "opencode": "ignored:no_roots",
    }


def test_config_show_workspace_applicability_is_active_for_existing_roots(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "existing").mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.existing]\n"
        'path = "./existing"\n',
        encoding="utf-8",
    )

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    assert result.workspace.roots.projected == 1
    assert result.workspace.roots.skipped == 0
    assert result.workspace.applicability == {
        "claude": "active",
        "codex": "active",
        "opencode": "active",
    }


def test_config_show_includes_state_retention_days_default(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    retention = next(item for item in result.values if item.key == "state.retention_days")

    assert retention.value == 30
    assert retention.source == "builtin"
    assert load_config(project_root).state.retention_days == 30


@pytest.mark.parametrize(
    ("source", "expected_source"),
    [
        ("env", "env var"),
        ("project", "file"),
        ("user", "user-config"),
    ],
)
def test_config_inspection_skips_model_resolution_for_defaults_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    expected_source: str,
) -> None:
    project_root = _repo(tmp_path)
    model = "gpt-5.4"
    monkeypatch.delenv("MERIDIAN_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)

    if source == "env":
        monkeypatch.setenv("MERIDIAN_DEFAULT_MODEL", model)
    elif source == "project":
        (project_root / "meridian.toml").write_text(
            f'[defaults]\nmodel = "{model}"\n',
            encoding="utf-8",
        )
    else:
        user_config = tmp_path / "user-config.toml"
        user_config.write_text(
            f'[defaults]\nmodel = "{model}"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())

    def _unexpected_resolve_model(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("config inspection should not call model resolution")

    monkeypatch.setattr(catalog_models, "resolve_model", _unexpected_resolve_model)

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    shown_value = next(item for item in shown.values if item.key == "defaults.model")
    gotten = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="defaults.model")
    )

    assert shown_value.value == model
    assert shown_value.source == expected_source
    assert gotten.value == model
    assert gotten.source == expected_source
    if expected_source == "env var":
        assert shown_value.env_var == "MERIDIAN_DEFAULT_MODEL"
        assert gotten.env_var == "MERIDIAN_DEFAULT_MODEL"
    else:
        assert shown_value.env_var is None
        assert gotten.env_var is None


def test_load_config_preserves_model_alias_tokens_without_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        '[defaults]\nmodel = "  gpt-fast  "\n'
        "[primary]\n"
        'model = "  reviewer-model  "\n'
        "[harness]\n"
        'codex = "  codex-default  "\n',
        encoding="utf-8",
    )

    def _unexpected_resolve_model(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("config loading should preserve model tokens without resolution")

    monkeypatch.setattr(catalog_models, "resolve_model", _unexpected_resolve_model)

    config = load_config(project_root)

    assert config.default_model == "gpt-fast"
    assert config.primary.model == "reviewer-model"
    assert config.harness.codex.model == "codex-default"


def test_load_config_prefers_meridian_local_toml_over_meridian_toml(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        '[defaults]\nharness = "claude"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        '[defaults]\nharness = "opencode"\n',
        encoding="utf-8",
    )

    assert load_config(project_root).default_harness == "opencode"


def test_config_get_resolves_aliases_through_option_catalog(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        '[defaults]\nmodel = "gpt-5.4"\n',
        encoding="utf-8",
    )

    result = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="default_model")
    )

    assert result.key == "defaults.model"
    assert result.value == "gpt-5.4"
    assert result.source == "file"


def test_config_show_and_get_support_nested_harness_model_table(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        '[harness.codex]\nmodel = "gpt-5.4"\n',
        encoding="utf-8",
    )

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    shown_value = next(item for item in shown.values if item.key == "harness.codex")
    gotten = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="harness.codex")
    )

    assert shown_value.value == "gpt-5.4"
    assert shown_value.source == "file"
    assert gotten.key == "harness.codex"
    assert gotten.value == "gpt-5.4"
    assert gotten.source == "file"
    assert load_config(project_root).harness.codex.model == "gpt-5.4"


def test_config_show_and_get_report_primary_autocompact_key(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[primary]\nautocompact = 72\n",
        encoding="utf-8",
    )

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    shown_value = next(item for item in shown.values if item.key == "primary.autocompact")
    gotten = config_get_sync(
        ConfigGetInput(project_root=project_root.as_posix(), key="primary.autocompact")
    )

    assert shown_value.value == 72
    assert shown_value.source == "file"
    assert gotten.key == "primary.autocompact"
    assert gotten.value == 72
    assert gotten.source == "file"
    assert load_config(project_root).primary.autocompact == 72


def test_config_show_and_get_report_spawn_wait_yield_keys(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[spawn]\n"
        "default_wait_yield_seconds = 120\n"
        "min_wait_yield_seconds = 45\n",
        encoding="utf-8",
    )

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
    assert shown_default.source == "file"
    assert shown_min.value == 45.0
    assert shown_min.source == "file"
    assert gotten_default.key == "spawn.default_wait_yield_seconds"
    assert gotten_default.value == 120.0
    assert gotten_default.source == "file"
    assert gotten_min.key == "spawn.min_wait_yield_seconds"
    assert gotten_min.value == 45.0
    assert gotten_min.source == "file"
    config = load_config(project_root)
    assert config.default_wait_yield_seconds == 120.0
    assert config.min_wait_yield_seconds == 45.0


def test_config_set_updates_primary_autocompact(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    config_path = project_root / "meridian.toml"
    config_path.write_text(
        "[primary]\n"
        "autocompact = 72 # keep legacy comment only until rewrite\n",
        encoding="utf-8",
    )

    result = config_set_sync(
        ConfigSetInput(
            project_root=project_root.as_posix(),
            key="primary.autocompact",
            value="81",
        )
    )

    assert result.key == "primary.autocompact"
    assert result.value == 81
    assert config_path.read_text(encoding="utf-8") == (
        "[primary]\n"
        "autocompact = 81 # keep legacy comment only until rewrite\n"
    )
    assert load_config(project_root).primary.autocompact == 81


def test_config_reset_removes_primary_autocompact(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    config_path = project_root / "meridian.toml"
    config_path.write_text(
        "[primary]\n"
        "autocompact = 72\n",
        encoding="utf-8",
    )

    result = config_reset_sync(
        ConfigResetInput(project_root=project_root.as_posix(), key="primary.autocompact")
    )

    assert result.removed is True
    assert config_path.read_text(encoding="utf-8") == ""
    assert load_config(project_root).primary.autocompact is None
