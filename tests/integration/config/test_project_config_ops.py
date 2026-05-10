from pathlib import Path

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.util import FormatContext, to_jsonable
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
    ensure_runtime_state_bootstrap_sync,
)
from meridian.lib.state.paths import resolve_project_runtime_root


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


def test_config_init_creates_meridian_toml_and_is_idempotent(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    config_path = project_root / "meridian.toml"

    first = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))
    config_path.write_text('[defaults]\nharness = "claude"\n', encoding="utf-8")
    second = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))

    assert first.created is True
    assert second.created is False
    assert first.path == config_path.as_posix()
    assert second.path == config_path.as_posix()
    assert config_path.is_file()
    assert config_path.read_text(encoding="utf-8") == '[defaults]\nharness = "claude"\n'
    assert not (project_root / "mars.toml").exists()
    assert not (project_root / ".mars").exists()


def test_config_init_scaffold_includes_dynamic_section_examples(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    result = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))
    content = Path(result.path).read_text(encoding="utf-8")

    assert result.created is True
    assert "[defaults]" in content
    assert "[state]" in content
    assert "# [agents.tech-lead]" in content
    assert "# [[hooks]]" in content
    assert "# [context.work]" in content
    assert "# [workspace.docs]" in content


def test_runtime_bootstrap_does_not_create_meridian_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    user_state_root = tmp_path / "user-state"
    monkeypatch.setenv("MERIDIAN_HOME", user_state_root.as_posix())

    ensure_runtime_state_bootstrap_sync(project_root)
    runtime_root = resolve_project_runtime_root(project_root)

    assert (project_root / ".meridian").is_dir()
    assert (project_root / ".meridian" / ".gitignore").is_file()
    assert not (project_root / ".meridian" / "artifacts").exists()
    assert not (project_root / ".meridian" / "cache").exists()
    assert not (project_root / ".meridian" / "spawns").exists()
    project_uuid = (project_root / ".meridian" / "id").read_text(encoding="utf-8").strip()
    assert runtime_root == user_state_root / "projects" / project_uuid
    assert runtime_root.is_dir()
    assert (runtime_root / "spawns").is_dir()
    assert not (project_root / "meridian.toml").exists()
    assert not (project_root / ".mars").exists()
    assert not (project_root / "mars.toml").exists()


def test_runtime_bootstrap_skips_context_dirs_for_git_backed_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed context directories are created by git-autosync hooks, not bootstrap.

    Bootstrap should skip creating these directories because:
    1. The clone doesn't exist yet (lazy clone approach)
    2. Creating dirs before clone would leave non-git directories at clone paths
    """
    project_root = _repo(tmp_path)
    remote = "https://example.com/acme/context.git"
    clone_path = tmp_path / "clones" / "context"
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'source = "git"',
                f'remote = "{remote}"',
                'path = ".meridian/work"',
                'archive = ".meridian/archive/work"',
                "",
                "[context.kb]",
                'source = "git"',
                f'remote = "{remote}"',
                'path = ".meridian/kb"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    def _resolve_clone_path(repo_url: str) -> Path:
        assert repo_url == remote
        return clone_path

    monkeypatch.setattr("meridian.lib.context.resolver.resolve_clone_path", _resolve_clone_path)

    ensure_runtime_state_bootstrap_sync(project_root)

    # Root .meridian directory is created
    assert (project_root / ".meridian").is_dir()

    # Git-backed context directories are NOT created (no clone exists yet)
    assert not clone_path.exists()
    assert not (clone_path / ".meridian" / "work").exists()
    assert not (clone_path / ".meridian" / "archive" / "work").exists()
    assert not (clone_path / ".meridian" / "kb").exists()


@pytest.mark.parametrize("remote_line", ["", 'remote = ""\n'])
def test_runtime_bootstrap_git_source_without_remote_falls_back_to_local_dirs(
    tmp_path: Path,
    remote_line: str,
) -> None:
    project_root = _repo(tmp_path)
    # Explicit project-local paths so assertions work regardless of default changes.
    # The key behavior under test: source=git with no remote should not attempt a
    # clone, and should create dirs at the configured paths instead.
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'source = "git"',
                'path = ".meridian/work"',
                'archive = ".meridian/archive/work"',
                remote_line.rstrip("\n"),
                "",
                "[context.kb]",
                'source = "git"',
                'path = ".meridian/kb"',
                remote_line.rstrip("\n"),
                "",
            ]
        ),
        encoding="utf-8",
    )

    ensure_runtime_state_bootstrap_sync(project_root)

    assert (project_root / ".meridian").is_dir()
    assert (project_root / ".meridian" / "work").is_dir()
    assert (project_root / ".meridian" / "archive" / "work").is_dir()
    assert (project_root / ".meridian" / "kb").is_dir()


def test_config_init_uses_env_project_root_when_path_not_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_project_root = _repo(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", env_project_root.as_posix())
    monkeypatch.chdir(cwd)

    result = config_init_sync(ConfigInitInput())

    assert result.path == (env_project_root / "meridian.toml").as_posix()
    assert (env_project_root / "meridian.toml").is_file()
    assert not (cwd / "meridian.toml").exists()
    assert not (env_project_root / "mars.toml").exists()


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


def test_config_show_surfaces_workspace_findings(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.local.toml").write_text(
        "[workspace.docs]\n"
        'path = "./missing-root"\n'
        'extra = "yes"\n',
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
        set(finding) == {"code", "message", "payload"}
        for finding in payload["workspace_findings"]
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
        "[defaults]\n"
        'harness = "opencode"\n'
        "\n"
        "[workspace.user_docs]\n"
        f'path = "{user_workspace_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config_path.as_posix())

    result = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    harness_value = next(item for item in result.values if item.key == "defaults.harness")

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
        "[workspace.docs]\n"
        'path = "./docs"\n'
        "\n"
        "[workspace.shared]\n"
        'path = "./committed-shared"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        "[workspace.shared]\n"
        'path = "./missing-shared"\n',
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

    shown = config_show_sync(
        ConfigShowInput(project_root=project_root.as_posix())
    )
    # write env after initial project-only check
    assert (
        next(item for item in shown.values if item.key == "agents.reviewer.model").source
        == "file"
    )
    assert (
        next(item for item in shown.values if item.key == "context.work.source").source
        == "file"
    )

    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())
    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))

    assert (
        next(item for item in shown.values if item.key == "agents.reviewer.model").source
        == "file"
    )
    assert (
        next(item for item in shown.values if item.key == "context.work.source").source
        == "file"
    )
    assert (
        next(item for item in shown.values if item.key == "context.work.remote").source
        == "user-config"
    )
    assert (
        next(item for item in shown.values if item.key == "context.kb.path").source
        == "user-config"
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


def test_config_show_reports_project_hook_suppression_over_lower_precedence_user_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text("hooks = []\n", encoding="utf-8")
    user_config = tmp_path / "user-config.toml"
    user_config.write_text(
        "[[hooks]]\n"
        'name = "user-hook"\n'
        'event = "spawn"\n'
        'command = "echo user"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())

    shown = config_show_sync(ConfigShowInput(project_root=project_root.as_posix()))
    hook_value = next(item for item in shown.values if item.key == "hooks")

    assert hook_value.value == "[] (suppressed)"
    assert hook_value.source == "file"
    assert "hooks: [] (suppressed) [source: file]" in shown.format_text(FormatContext())


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
