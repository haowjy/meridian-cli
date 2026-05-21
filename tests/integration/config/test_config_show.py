# qa-validated: test-suite-redesign
"""Config show operation tests — workspace findings, provenance, hook suppression."""

from pathlib import Path

import pytest

from meridian.lib.core.util import FormatContext, to_jsonable
from meridian.lib.ops.config import (
    ConfigShowInput,
    config_show_sync,
)


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def test_config_show_surfaces_workspace_findings(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.local.toml").write_text(
        '[workspace.docs]\npath = "./missing-root"\nextra = "yes"\n',
        encoding="utf-8",
    )

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    assert result.workspace.status == "present"
    assert result.workspace.sources == (
        (project_root / "meridian.local.toml").resolve().as_posix(),
    )
    assert result.workspace.roots.count == 1
    assert result.workspace.roots.projected == 0
    assert result.workspace.roots.skipped == 1
    finding_codes = {finding.code for finding in result.workspace_findings}
    assert finding_codes == {
        "workspace_unknown_key",
        "workspace_local_missing_root",
    }
    payload = to_jsonable(result)
    assert {finding["code"] for finding in payload["workspace_findings"]} == finding_codes
    assert all(
        set(finding) == {"code", "message", "payload"} for finding in payload["workspace_findings"]
    )
    text = result.format_text()
    assert "warning: workspace_unknown_key:" in text
    assert "warning: workspace_local_missing_root:" in text


def test_config_show_ignores_user_global_workspace_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    user_workspace_root = tmp_path / "user-workspace-root"
    user_workspace_root.mkdir()
    user_config_path = tmp_path / "user-config.toml"
    user_config_path.write_text(
        "[primary]\n"
        'harness = "opencode"\n'
        "\n"
        "[workspace.user_docs]\n"
        f'path = "{user_workspace_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config_path.as_posix())

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    harness_value = next(item for item in result.values if item.key == "primary.harness")

    assert result.workspace.status == "none"
    assert result.workspace.sources == ()
    assert result.workspace.roots.count == 0
    assert result.workspace.roots.projected == 0
    assert result.workspace.roots.skipped == 0
    assert result.workspace_findings == ()
    assert harness_value.value == "opencode"
    assert harness_value.source == "user-config"


def test_config_show_verbose_and_json_include_named_workspace_root_details(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    docs_root = project_root / "docs"
    committed_shared_root = project_root / "committed-shared"
    docs_root.mkdir()
    committed_shared_root.mkdir()
    (project_root / "meridian.toml").write_text(
        '[workspace.docs]\npath = "./docs"\n\n[workspace.shared]\npath = "./committed-shared"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        '[workspace.shared]\npath = "./missing-shared"\n',
        encoding="utf-8",
    )

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    assert result.workspace.status == "present"
    assert result.workspace.roots.count == 2
    assert result.workspace.roots.projected == 1
    assert result.workspace.roots.skipped == 1
    assert result.workspace.applicability == {
        "claude": "active",
        "codex": "active",
        "opencode": "active",
    }
    assert [root.name for root in result.workspace.roots_detail] == ["docs", "shared"]
    assert [root.source for root in result.workspace.roots_detail] == ["committed", "merged"]
    assert [root.status for root in result.workspace.roots_detail] == ["projected", "skipped"]

    payload = to_jsonable(result)
    assert payload["workspace"]["roots_detail"] == [
        {
            "name": "docs",
            "source": "committed",
            "declared_path": "./docs",
            "resolved_path": docs_root.resolve().as_posix(),
            "status": "projected",
        },
        {
            "name": "shared",
            "source": "merged",
            "declared_path": "./missing-shared",
            "resolved_path": (project_root / "missing-shared").resolve().as_posix(),
            "status": "skipped",
        },
    ]

    text = result.format_text(FormatContext(verbosity=1))
    assert "workspace.applicability.claude = active" in text
    assert "workspace.applicability.codex = active" in text
    assert "workspace.applicability.opencode = active" in text
    assert "workspace.roots[0].name = docs" in text
    assert "workspace.roots[0].status = projected" in text
    assert "workspace.roots[1].name = shared" in text
    assert "workspace.roots[1].status = skipped" in text
    assert "warning: workspace_local_missing_root:" in text


def test_config_show_attributes_dynamic_sections_from_file_and_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[context.work]\n"
        'source = "git"\n'
        "\n"
        "[agents.reviewer]\n"
        'model = "gpt55"\n'
        "\n"
        "[[hooks]]\n"
        'event = "spawn"\n'
        'command = "echo project"\n',
        encoding="utf-8",
    )
    user_config = tmp_path / "user-config.toml"
    user_config.write_text(
        "[context.work]\n"
        'remote = "https://example.com/work.git"\n'
        "\n"
        "[context.kb]\n"
        'path = "./kb"\n'
        "\n"
        "[work.artifacts]\n"
        'sync = "project"\n',
        encoding="utf-8",
    )

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    # write env after initial project-only check
    assert (
        next(item for item in shown.values if item.key == "agents.reviewer.model").source == "file"
    )
    assert next(item for item in shown.values if item.key == "context.work.source").source == "file"

    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())
    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    assert (
        next(item for item in shown.values if item.key == "agents.reviewer.model").source == "file"
    )
    assert next(item for item in shown.values if item.key == "context.work.source").source == "file"
    assert (
        next(item for item in shown.values if item.key == "context.work.remote").source
        == "user-config"
    )
    assert (
        next(item for item in shown.values if item.key == "context.kb.path").source == "user-config"
    )
    assert (
        next(item for item in shown.values if item.key == "work.artifacts.sync").source
        == "user-config"
    )
    assert next(item for item in shown.values if item.key == "hooks").source == "file"


def test_config_show_reports_project_hook_suppression_with_file_provenance(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text("hooks = []\n", encoding="utf-8")

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    hook_value = next(item for item in shown.values if item.key == "hooks")

    assert hook_value.value == "[] (suppressed)"
    assert hook_value.source == "file"
    assert "hooks: [] (suppressed) [source: file]" in shown.format_text(FormatContext())


def test_config_show_reports_user_hook_suppression_with_user_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    user_config = tmp_path / "user-config.toml"
    user_config.write_text("hooks = []\n", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    hook_value = next(item for item in shown.values if item.key == "hooks")

    assert hook_value.value == "[] (suppressed)"
    assert hook_value.source == "user-config"
    assert "hooks: [] (suppressed) [source: user-config]" in shown.format_text(FormatContext())


def test_config_show_reports_empty_overlay_model_policy_list(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[agents.reviewer]\nmodel-policies = []\n",
        encoding="utf-8",
    )

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    policy_value = next(
        item for item in shown.values if item.key == "agents.reviewer.model-policies"
    )

    assert policy_value.value == "[] (no overlay rules)"
    assert policy_value.source == "file"
    assert (
        "agents.reviewer.model-policies: [] (no overlay rules) [source: file]"
        in shown.format_text(FormatContext())
    )


def test_config_show_reports_project_hook_suppression_over_lower_precedence_user_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text("hooks = []\n", encoding="utf-8")
    user_config = tmp_path / "user-config.toml"
    user_config.write_text(
        '[[hooks]]\nname = "user-hook"\nevent = "spawn"\ncommand = "echo user"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    hook_value = next(item for item in shown.values if item.key == "hooks")

    assert hook_value.value == "[] (suppressed)"
    assert hook_value.source == "file"
    assert "hooks: [] (suppressed) [source: file]" in shown.format_text(FormatContext())
