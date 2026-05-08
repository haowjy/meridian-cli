"""Workspace smoke tests — init and inspection.

Replaces: tests/e2e/workspace/init-inspection.md
"""

import json


def _failure_json(result):
    return json.loads(result.stderr)


def test_workspace_init_updates_local_config_and_is_idempotent(cli_with_git, scratch_dir):
    """workspace init appends local scaffold, updates gitignore, and is safe to rerun."""
    local_path = scratch_dir / "meridian.local.toml"
    local_path.write_text('model = "gpt-5"\n', encoding="utf-8")

    first = cli_with_git("workspace", "init")
    first.assert_success()

    content = local_path.read_text(encoding="utf-8")
    exclude_lines = (scratch_dir / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    ).splitlines()

    assert "created:" in first.stdout
    assert "local_gitignore:" in first.stdout
    assert "(updated)" in first.stdout
    assert 'model = "gpt-5"' in content
    assert content.count("[workspace.example]") == 1
    assert exclude_lines.count("meridian.local.toml") == 1

    second = cli_with_git("workspace", "init")
    second.assert_success()

    assert "exists:" in second.stdout
    assert "(ok)" in second.stdout
    assert local_path.read_text(encoding="utf-8").count("[workspace.example]") == 1


def test_config_show_surfaces_workspace_status(cli_with_git):
    """config show reports the post-init workspace surface."""
    cli_with_git("workspace", "init")

    result = cli_with_git("config", "show", json_mode=True)
    result.assert_success()
    data = result.json

    assert data["workspace"]["status"] == "none"
    assert data["workspace"]["sources"] == []
    assert data["workspace"]["roots"] == {"count": 0, "projected": 0, "skipped": 0}
    assert data["workspace_findings"] == []


def test_doctor_surfaces_missing_local_workspace_root_warning(cli_with_git, scratch_dir):
    """doctor reports actionable workspace warnings for a missing local root."""
    (scratch_dir / ".mars" / "agents").mkdir(parents=True, exist_ok=True)
    (scratch_dir / ".mars" / "skills").mkdir(parents=True, exist_ok=True)
    (scratch_dir / "meridian.local.toml").write_text(
        "[workspace.missing]\npath = \"./missing-local\"\n",
        encoding="utf-8",
    )

    result = cli_with_git("doctor", json_mode=True)
    result.assert_success()
    data = result.json

    workspace_warning = next(
        warning
        for warning in data["warnings"]
        if warning["code"] == "workspace_local_missing_root"
    )

    assert data["ok"] is False
    assert workspace_warning["payload"]["name"] == "missing"
    assert workspace_warning["payload"]["path"].endswith("/missing-local")
    assert "does not exist" in workspace_warning["message"]


def test_invalid_named_workspace_blocks_spawn_dry_run_with_actionable_guidance(
    cli_with_git, scratch_dir
):
    """Invalid named workspace config blocks launch and points to diagnosis commands."""
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "test.md").write_text("# Test\n", encoding="utf-8")
    (scratch_dir / "meridian.toml").write_text(
        "[workspace.Bad]\npath = \"./root\"\n",
        encoding="utf-8",
    )

    result = cli_with_git("spawn", "-a", "test", "-p", "test", "--dry-run", json_mode=True)
    result.assert_failure()
    error = _failure_json(result)["error"]

    assert "Invalid workspace config in meridian.toml." in error
    assert "entry name 'Bad'" in error
    assert "meridian config show" in error
    assert "meridian doctor" in error
