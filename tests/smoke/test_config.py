"""Config command smoke tests — init, show, get, set, reset.

Replaces: tests/e2e/config/init-show-set.md
"""

import json
from pathlib import Path


def _posix_str(p: Path | str) -> str:
    """Normalize path to forward slashes for CLI output comparison."""
    return str(p).replace("\\", "/")


def test_config_init_creates_file(cli_with_git, scratch_dir):
    """config init creates meridian.toml."""
    result = cli_with_git("config", "init")
    result.assert_success()
    config_file = scratch_dir / "meridian.toml"
    assert result.stdout.strip() == f"created: {_posix_str(config_file)}"
    assert config_file.exists()


def test_config_show_exposes_values(cli, scratch_dir):
    """config show reports resolved roots and built-in defaults."""
    result = cli("config", "show", json_mode=True)
    result.assert_success()
    payload = json.loads(result.stdout)
    values = {row["key"]: row for row in payload["values"]}
    assert payload["project_root"] == _posix_str(scratch_dir)
    assert payload["project_root_source"] == "env"
    assert payload["runtime_root"] is None
    assert values["defaults.harness"]["value"] == "codex"
    assert values["defaults.harness"]["source"] == "builtin"


def test_config_get_reads_single_key(cli):
    """config get returns a resolved key."""
    result = cli("config", "get", "defaults.harness")
    result.assert_success()
    assert result.stdout.strip() == "defaults.harness: codex [source: builtin]"


def test_config_set_and_get_roundtrip(cli_with_git, scratch_dir):
    """config set persists an override that config get can read."""
    cli_with_git("config", "init")
    set_result = cli_with_git("config", "set", "defaults.model", "smoke-test-model")
    set_result.assert_success()
    get_result = cli_with_git("config", "get", "defaults.model")
    get_result.assert_success()
    assert "set defaults.model = smoke-test-model" in set_result.stdout
    assert get_result.stdout.strip() == "defaults.model: smoke-test-model [source: file]"
    assert 'model = "smoke-test-model"' in (scratch_dir / "meridian.toml").read_text(
        encoding="utf-8"
    )


def test_config_reset_removes_override(cli_with_git):
    """config reset removes a previously set override."""
    cli_with_git("config", "init")
    cli_with_git("config", "set", "defaults.model", "to-be-reset")
    result = cli_with_git("config", "reset", "defaults.model")
    result.assert_success()
    assert "reset defaults.model (removed)" in result.stdout
    get_result = cli_with_git("config", "get", "defaults.model")
    get_result.assert_success()
    assert get_result.stdout.strip() == "defaults.model:  [source: builtin]"


def test_config_set_preserves_dynamic_sections(cli_with_git, scratch_dir):
    """config set does not erase unrelated dynamic sections."""
    config_path = scratch_dir / "meridian.toml"
    config_path.write_text(
        "[defaults]\n"
        'harness = "claude"\n'
        "\n"
        "[workspace.docs]\n"
        'path = "./docs"\n'
        "\n"
        "[[hooks]]\n"
        'event = "spawn"\n'
        'run = "echo hi"\n',
        encoding="utf-8",
    )

    result = cli_with_git("config", "set", "defaults.harness", "opencode")

    result.assert_success()
    updated = config_path.read_text(encoding="utf-8")
    assert 'harness = "opencode"' in updated
    assert "[workspace.docs]" in updated
    assert "[[hooks]]" in updated
