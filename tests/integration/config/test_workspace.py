from pathlib import Path

import pytest

from meridian.lib.config.workspace import (
    get_projectable_roots,
    resolve_workspace_snapshot,
)
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.launch.workspace import ensure_workspace_valid_for_launch
from meridian.lib.ops.runtime import resolve_project_authority


@pytest.fixture(autouse=True)
def _clear_state_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


def _build_workspace_launch_context(
    *,
    project_root: Path,
    surface: LaunchCompositionSurface = LaunchCompositionSurface.DIRECT,
):
    request = SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt="hello",
    )
    runtime = LaunchRuntime(
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
        composition_surface=surface,
        report_output_path=(project_root / "report.md").as_posix(),
        runtime_root=(project_root / ".meridian").as_posix(),
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=project_root.as_posix(),
    )
    return build_launch_context(
        spawn_id="p-workspace",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )


def test_resolve_workspace_snapshot_is_none_when_workspace_file_absent(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "none"
    assert snapshot.source_paths == ()
    assert snapshot.roots == ()
    assert snapshot.findings == ()


def test_workspace_snapshot_resolves_paths_relative_to_workspace_file(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    sibling_root = tmp_path / "sibling"
    sibling_root.mkdir()
    workspace_path = project_root / "workspace.local.toml"
    workspace_path.write_text(
        "[[context-roots]]\n"
        'path = "../sibling"\n'
        "\n"
        "[[context-roots]]\n"
        'path = "./disabled-missing"\n'
        "enabled = false\n",
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    assert snapshot.source_paths == (workspace_path.resolve(),)
    assert [root.name for root in snapshot.roots] == ["legacy-1", "legacy-2"]
    assert [root.source for root in snapshot.roots] == ["legacy", "legacy"]
    assert [root.declared_path for root in snapshot.roots] == [
        "../sibling",
        "./disabled-missing",
    ]
    assert snapshot.roots[0].resolved_path == sibling_root.resolve()
    assert snapshot.roots[0].enabled is True
    assert snapshot.roots[0].exists is True
    assert snapshot.roots[1].resolved_path == (project_root / "disabled-missing").resolve()
    assert snapshot.roots[1].enabled is False
    assert snapshot.roots[1].exists is False
    assert snapshot.missing_roots_count == 0
    assert {finding.code for finding in snapshot.findings} == {
        "workspace_deprecated_legacy",
        "workspace_legacy_file_present",
    }


def test_get_projectable_roots_returns_only_enabled_existing_entries(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    existing = project_root / "existing"
    existing.mkdir()
    (project_root / "workspace.local.toml").write_text(
        "[[context-roots]]\n"
        'path = "./existing"\n'
        "\n"
        "[[context-roots]]\n"
        'path = "./missing"\n'
        "\n"
        "[[context-roots]]\n"
        'path = "./disabled"\n'
        "enabled = false\n",
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert get_projectable_roots(snapshot) == (existing.resolve(),)


def test_workspace_snapshot_surfaces_unknown_keys_and_missing_enabled_roots(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    workspace_path = project_root / "workspace.local.toml"
    workspace_path.write_text(
        'future = "value"\n'
        "[[context-roots]]\n"
        'path = "./missing-root"\n'
        'comment = "kept"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    finding_codes = {finding.code for finding in snapshot.findings}
    assert finding_codes == {
        "workspace_unknown_key",
        "workspace_missing_root",
        "workspace_deprecated_legacy",
        "workspace_legacy_file_present",
    }
    unknown = next(f for f in snapshot.findings if f.code == "workspace_unknown_key")
    assert unknown.payload == {"keys": ["future", "context-roots[1].comment"]}
    missing = next(f for f in snapshot.findings if f.code == "workspace_missing_root")
    assert missing.payload == {"roots": [(project_root / "missing-root").resolve().as_posix()]}


def test_workspace_snapshot_reads_legacy_workspace_from_project_root(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    workspace_path = project_root / "workspace.local.toml"
    (project_root / "shared-root").mkdir()
    workspace_path.write_text(
        "[[context-roots]]\n"
        'path = "./shared-root"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    assert snapshot.source_paths == (workspace_path.resolve(),)
    assert snapshot.roots[0].resolved_path == (project_root / "shared-root").resolve()


def test_workspace_snapshot_uses_frozen_authority_config_paths(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    first_workspace_root = tmp_path / "state-a"
    second_workspace_root = tmp_path / "state-b"
    for workspace_root in (first_workspace_root, second_workspace_root):
        workspace_root.mkdir(parents=True)
        (workspace_root / "shared-root").mkdir()
    first_workspace_path = first_workspace_root / "workspace.local.toml"
    second_workspace_path = second_workspace_root / "workspace.local.toml"
    first_workspace_path.write_text(
        "[[context-roots]]\n"
        'path = "./shared-root"\n',
        encoding="utf-8",
    )
    authority = resolve_project_authority(project_root)
    frozen_paths = authority.project_config_paths.model_copy(
        update={"workspace_local_toml": first_workspace_path}
    )
    frozen_authority = authority.model_copy(update={"project_config_paths": frozen_paths})

    second_workspace_path.write_text(
        "[[context-roots]]\n"
        'path = "./shared-root"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(
        project_root,
        config_paths=frozen_authority.project_config_paths,
    )

    assert snapshot.status == "present"
    assert snapshot.source_paths == (first_workspace_path.resolve(),)
    assert snapshot.roots[0].resolved_path == (first_workspace_root / "shared-root").resolve()


def test_workspace_snapshot_loads_named_committed_and_local_workspace_entries(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    frontend_committed = tmp_path / "frontend-committed"
    frontend_local = tmp_path / "frontend-local"
    prompts = tmp_path / "prompts"
    data = tmp_path / "data"
    for path in (frontend_committed, frontend_local, prompts, data):
        path.mkdir()
    committed_path = project_root / "meridian.toml"
    local_path = project_root / "meridian.local.toml"
    committed_path.write_text(
        "[workspace.frontend]\n"
        'path = "../frontend-committed"\n'
        "\n"
        "[workspace.prompts]\n"
        'path = "../prompts"\n',
        encoding="utf-8",
    )
    local_path.write_text(
        "[workspace.frontend]\n"
        'path = "../frontend-local"\n'
        "\n"
        "[workspace.data]\n"
        'path = "../data"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    assert snapshot.source_paths == (committed_path.resolve(), local_path.resolve())
    assert [root.name for root in snapshot.roots] == ["frontend", "prompts", "data"]
    assert [root.source for root in snapshot.roots] == ["merged", "committed", "local"]
    assert [root.resolved_path for root in snapshot.roots] == [
        frontend_local.resolve(),
        prompts.resolve(),
        data.resolve(),
    ]
    assert get_projectable_roots(snapshot) == (
        frontend_local.resolve(),
        prompts.resolve(),
        data.resolve(),
    )
    assert snapshot.findings == ()


def test_user_global_workspace_config_is_ignored_for_snapshot_and_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    user_workspace_root = tmp_path / "user-workspace-root"
    user_workspace_root.mkdir()
    user_config_path = tmp_path / "user-config.toml"
    user_config_path.write_text(
        "[workspace.user_docs]\n"
        f'path = "{user_workspace_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config_path.as_posix())

    snapshot = resolve_workspace_snapshot(project_root)
    runtime_ctx = _build_workspace_launch_context(project_root=project_root)

    assert snapshot.status == "none"
    assert snapshot.source_paths == ()
    assert snapshot.roots == ()
    assert snapshot.findings == ()
    assert user_workspace_root.as_posix() not in runtime_ctx.env_overrides.values()
    assert all(
        user_workspace_root.as_posix() not in arg
        for arg in runtime_ctx.run_params.extra_args
    )


def test_workspace_snapshot_skips_committed_missing_and_reports_local_missing(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    existing = project_root / "existing"
    existing.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.committed_missing]\n"
        'path = "./missing-committed"\n'
        "\n"
        "[workspace.existing]\n"
        'path = "./existing"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        "[workspace.local_missing]\n"
        'path = "./missing-local"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert get_projectable_roots(snapshot) == (existing.resolve(),)
    assert [finding.code for finding in snapshot.findings] == [
        "workspace_local_missing_root"
    ]
    assert snapshot.findings[0].payload == {
        "name": "local_missing",
        "path": (project_root / "missing-local").resolve().as_posix(),
    }


def test_named_workspace_projectable_roots_are_existing_only_in_deterministic_order(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    first = project_root / "first"
    second_committed = project_root / "second-committed"
    second_local = project_root / "second-local"
    local_only = project_root / "local-only"
    for path in (first, second_committed, second_local, local_only):
        path.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.first]\n"
        'path = "./first"\n'
        "\n"
        "[workspace.committed_missing]\n"
        'path = "./missing-committed"\n'
        "\n"
        "[workspace.second]\n"
        'path = "./second-committed"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        "[workspace.second]\n"
        'path = "./second-local"\n'
        "\n"
        "[workspace.local_missing]\n"
        'path = "./missing-local"\n'
        "\n"
        "[workspace.local_only]\n"
        'path = "./local-only"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert [root.name for root in snapshot.roots] == [
        "first",
        "committed_missing",
        "second",
        "local_missing",
        "local_only",
    ]
    assert get_projectable_roots(snapshot) == (
        first.resolve(),
        second_local.resolve(),
        local_only.resolve(),
    )


def test_workspace_snapshot_reports_merged_missing_as_local_missing(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    committed_root = project_root / "committed-root"
    committed_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.shared]\n"
        'path = "./committed-root"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        "[workspace.shared]\n"
        'path = "./missing-local-override"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert [root.name for root in snapshot.roots] == ["shared"]
    assert [root.source for root in snapshot.roots] == ["merged"]
    assert get_projectable_roots(snapshot) == ()
    assert [finding.code for finding in snapshot.findings] == [
        "workspace_local_missing_root"
    ]
    assert snapshot.findings[0].payload == {
        "name": "shared",
        "path": (project_root / "missing-local-override").resolve().as_posix(),
    }


def test_local_named_workspace_takes_precedence_over_legacy_file(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    local_root = project_root / "local-root"
    local_root.mkdir()
    (project_root / "meridian.local.toml").write_text(
        "[workspace.local]\n"
        'path = "./local-root"\n',
        encoding="utf-8",
    )
    (project_root / "workspace.local.toml").write_text(
        "[[context-roots]]\n"
        'path = "./legacy"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert [root.name for root in snapshot.roots] == ["local"]
    assert [root.source for root in snapshot.roots] == ["local"]
    assert [finding.code for finding in snapshot.findings] == [
        "workspace_legacy_file_present"
    ]


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        ("[workspace]\nfoo = 'bar'\n", "'workspace.foo'"),
        ("[workspace.Bad]\npath = './root'\n", "entry name 'Bad'"),
        ("[workspace.bad]\n", "'workspace.bad.path'"),
        ("[workspace.bad]\npath = '   '\n", "'workspace.bad.path'"),
        ("[workspace.bad]\npath = 123\n", "'workspace.bad.path'"),
    ],
)
def test_named_workspace_snapshot_marks_invalid_schema_cases(
    tmp_path: Path,
    content: str,
    expected_message: str,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(content, encoding="utf-8")

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "invalid"
    assert snapshot.findings[0].code == "workspace_invalid"
    assert expected_message in snapshot.findings[0].message


def test_named_workspace_invalid_schema_reports_failing_layer_path(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    committed_path = project_root / "meridian.toml"
    local_path = project_root / "meridian.local.toml"
    committed_path.write_text(
        "[workspace.good]\n"
        'path = "./good"\n',
        encoding="utf-8",
    )
    local_path.write_text(
        "[workspace.bad]\n"
        "path = 123\n",
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "invalid"
    assert snapshot.source_paths == (local_path.resolve(),)
    assert snapshot.findings[0].code == "workspace_invalid"
    assert local_path.as_posix() in snapshot.findings[0].message


def test_named_workspace_unknown_entry_keys_are_findings(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "root").mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.root]\n"
        'path = "./root"\n'
        "enabled = true\n",
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    assert snapshot.findings[0].code == "workspace_unknown_key"
    assert snapshot.findings[0].payload == {"keys": ["workspace.root.enabled"]}


def test_named_workspace_resolves_absolute_paths(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    absolute_root = tmp_path / "absolute-workspace-root"
    absolute_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.absolute]\n"
        f'path = "{absolute_root.as_posix()}"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    assert [root.name for root in snapshot.roots] == ["absolute"]
    assert snapshot.roots[0].declared_path == absolute_root.as_posix()
    assert snapshot.roots[0].resolved_path == absolute_root.resolve()
    assert snapshot.roots[0].exists is True
    assert get_projectable_roots(snapshot) == (absolute_root.resolve(),)
    assert snapshot.findings == ()


def test_named_workspace_expands_tilde_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = _repo(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    tilde_root = fake_home / "tilde-workspace-root"
    tilde_root.mkdir()
    monkeypatch.setenv("HOME", fake_home.as_posix())
    (project_root / "meridian.local.toml").write_text(
        "[workspace.tilde]\n"
        'path = "~/tilde-workspace-root"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    assert [root.name for root in snapshot.roots] == ["tilde"]
    assert snapshot.roots[0].declared_path == "~/tilde-workspace-root"
    assert snapshot.roots[0].resolved_path == tilde_root.resolve()
    assert snapshot.roots[0].exists is True
    assert get_projectable_roots(snapshot) == (tilde_root.resolve(),)
    assert snapshot.findings == ()


def test_named_workspace_takes_precedence_over_legacy_file(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    named = project_root / "named"
    named.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.named]\n"
        'path = "./named"\n',
        encoding="utf-8",
    )
    (project_root / "workspace.local.toml").write_text(
        "[[context-roots]]\n"
        'path = "./legacy"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert [root.name for root in snapshot.roots] == ["named"]
    assert [finding.code for finding in snapshot.findings] == [
        "workspace_legacy_file_present"
    ]


def test_workspace_entries_do_not_create_context_env_vars(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    workspace_root = project_root / "workspace-docs"
    workspace_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.docs]\n"
        'path = "./workspace-docs"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)
    runtime_ctx = _build_workspace_launch_context(project_root=project_root)

    assert get_projectable_roots(snapshot) == (workspace_root.resolve(),)
    assert "MERIDIAN_CONTEXT_DOCS_DIR" not in runtime_ctx.env_overrides
    assert workspace_root.as_posix() not in {
        value
        for key, value in runtime_ctx.env_overrides.items()
        if key.startswith("MERIDIAN_CONTEXT_") and key.endswith("_DIR")
    }


def test_workspace_entries_do_not_enter_system_prompt_context(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    _write_minimal_mars_config(project_root)
    workspace_root = project_root / "workspace-docs"
    workspace_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.docs]\n"
        'path = "./workspace-docs"\n',
        encoding="utf-8",
    )

    runtime_ctx = _build_workspace_launch_context(
        project_root=project_root,
        surface=LaunchCompositionSurface.PRIMARY,
    )
    system_prompt = runtime_ctx.run_params.appended_system_prompt or ""

    assert "workspace-docs" not in system_prompt
    assert "MERIDIAN_CONTEXT_DOCS_DIR" not in system_prompt


def test_workspace_snapshot_is_invalid_when_legacy_workspace_path_is_directory(
    tmp_path: Path,
) -> None:
    project_root = _repo(tmp_path)
    workspace_path = project_root / "workspace.local.toml"
    workspace_path.mkdir()

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "invalid"
    assert snapshot.source_paths == (workspace_path.resolve(),)
    assert snapshot.findings[0].code == "workspace_invalid"
    assert "exists but is not a file" in snapshot.findings[0].message
    assert snapshot.findings[0].payload == {"path": workspace_path.resolve().as_posix()}


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        (
            "[[context-roots]\npath = './bad'\n",
            "Invalid workspace TOML",
        ),
        (
            "[[context-roots]]\nenabled = true\n",
            "'context-roots[1].path' is required",
        ),
        (
            "[[context-roots]]\npath = '   '\n",
            "'context-roots[1].path' must be non-empty",
        ),
        (
            "[[context-roots]]\npath = 123\n",
            "'context-roots[1].path' must be a string",
        ),
        (
            "[[context-roots]]\npath = './root'\nenabled = 'yes'\n",
            "'context-roots[1].enabled' must be a boolean",
        ),
    ],
)
def test_workspace_snapshot_marks_invalid_schema_cases(
    tmp_path: Path,
    content: str,
    expected_message: str,
) -> None:
    project_root = _repo(tmp_path)
    workspace_path = project_root / "workspace.local.toml"
    workspace_path.write_text(content, encoding="utf-8")

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "invalid"
    assert snapshot.source_paths == (workspace_path.resolve(),)
    assert snapshot.findings
    assert snapshot.findings[0].code == "workspace_invalid"
    assert expected_message in snapshot.findings[0].message


def test_workspace_launch_validation_raises_for_invalid_workspace(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "workspace.local.toml").write_text("[[context-roots]]\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Invalid workspace config in workspace\.local\.toml"):
        ensure_workspace_valid_for_launch(project_root)


def test_workspace_launch_validation_allows_absent_or_valid_workspace(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    ensure_workspace_valid_for_launch(project_root)

    (project_root / "existing").mkdir()
    (project_root / "workspace.local.toml").write_text(
        "[[context-roots]]\n"
        'path = "./existing"\n',
        encoding="utf-8",
    )
    ensure_workspace_valid_for_launch(project_root)


@pytest.mark.parametrize(
    ("filename", "content", "expected_source", "expected_detail"),
    [
        (
            "meridian.toml",
            "[workspace.bad\npath = './root'\n",
            "meridian.toml",
            "Invalid TOML",
        ),
        (
            "meridian.local.toml",
            "[workspace.bad]\n",
            "meridian.local.toml",
            "'workspace.bad.path'",
        ),
    ],
)
def test_named_workspace_malformed_config_blocks_launch(
    tmp_path: Path,
    filename: str,
    content: str,
    expected_source: str,
    expected_detail: str,
) -> None:
    project_root = _repo(tmp_path)
    (project_root / filename).write_text(content, encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        ensure_workspace_valid_for_launch(project_root)

    message = str(exc_info.value)
    assert f"Invalid workspace config in {expected_source}." in message
    assert expected_detail in message
    assert "meridian config show" in message
    assert "meridian doctor" in message


def test_named_workspace_launch_validation_allows_valid_workspace(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "existing").mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.existing]\n"
        'path = "./existing"\n',
        encoding="utf-8",
    )

    ensure_workspace_valid_for_launch(project_root)


def test_named_workspace_empty_path_blocks_launch(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[workspace.empty]\n"
        'path = "  "\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"Invalid workspace config in meridian\.toml"):
        ensure_workspace_valid_for_launch(project_root)


def test_legacy_workspace_fallback_launch_validation(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "legacy-root").mkdir()
    (project_root / "workspace.local.toml").write_text(
        "[[context-roots]]\n"
        'path = "./legacy-root"\n',
        encoding="utf-8",
    )

    ensure_workspace_valid_for_launch(project_root)
