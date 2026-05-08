from pathlib import Path

import pytest

from meridian.lib.config.settings import OPTION_CATALOG, load_config


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


def test_option_catalog_resolves_canonical_and_alias_keys() -> None:
    assert OPTION_CATALOG.resolve_key("defaults.model").canonical_key == "defaults.model"
    assert OPTION_CATALOG.resolve_key("defaults.default_model").canonical_key == "defaults.model"
    assert OPTION_CATALOG.resolve_key("default_model").canonical_key == "defaults.model"
    assert (
        OPTION_CATALOG.resolve_key("wait_timeout_minutes").canonical_key
        == "timeouts.wait_minutes"
    )
    assert (
        OPTION_CATALOG.resolve_key("state.retention_days").canonical_key
        == "state.retention_days"
    )
    assert OPTION_CATALOG.resolve_key("primary.autocompact").canonical_key == (
        "primary.autocompact_pct"
    )
    assert OPTION_CATALOG.resolve_key("harness.codex.model").canonical_key == "harness.codex"


def test_load_config_uses_metadata_aliases_for_scalar_runtime_fields(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "kill_grace_minutes = 0.5\n"
        "guardrail_timeout_minutes = 3\n"
        "[defaults]\n"
        'default_model = "gpt-5.4-mini"\n',
        encoding="utf-8",
    )

    config = load_config(project_root)

    assert config.kill_grace_minutes == 0.5
    assert config.guardrail_timeout_minutes == 3.0
    assert config.default_model == "gpt-5.4-mini"
