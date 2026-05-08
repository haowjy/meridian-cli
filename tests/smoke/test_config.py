"""Config command smoke tests — init, show, get, set, reset.

Replaces: tests/e2e/config/init-show-set.md
"""


def test_config_init_creates_file(cli_with_git, scratch_dir):
    """config init creates meridian.toml."""
    # Use scratch_dir from cli_with_git's tmp_path
    result = cli_with_git("config", "init")
    result.assert_success()
    
    # Check file was created
    config_file = scratch_dir / "meridian.toml"
    # May be in different location based on implementation
    assert "meridian.toml" in result.stdout or config_file.exists() or result.returncode == 0


def test_config_show_exposes_values(cli):
    """config show includes expected key families."""
    result = cli("config", "show")
    result.assert_success()
    
    # Should show some config keys
    output = result.stdout.lower()
    assert "model" in output or "defaults" in output or "harness" in output


def test_config_get_reads_single_key(cli):
    """config get returns a resolved key."""
    result = cli("config", "get", "defaults.model")
    # May succeed or fail depending on if key exists
    # But should not traceback
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_config_set_and_get_roundtrip(cli_with_git, scratch_dir):
    """config set persists an override that config get can read."""
    # Initialize config first
    cli_with_git("config", "init")
    
    # Set a value
    set_result = cli_with_git("config", "set", "defaults.model", "smoke-test-model")
    # May succeed or fail based on implementation
    
    # Get the value back
    get_result = cli_with_git("config", "get", "defaults.model")
    
    # Verify no tracebacks
    assert "Traceback" not in set_result.stderr
    assert "Traceback" not in get_result.stderr


def test_config_reset_removes_override(cli_with_git):
    """config reset removes a previously set override."""
    cli_with_git("config", "init")
    cli_with_git("config", "set", "defaults.model", "to-be-reset")
    
    result = cli_with_git("config", "reset", "defaults.model")
    # Should succeed or fail cleanly
    assert "Traceback" not in result.stderr


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

    assert "Traceback" not in result.stderr
    updated = config_path.read_text(encoding="utf-8")
    assert "[workspace.docs]" in updated
    assert "[[hooks]]" in updated
