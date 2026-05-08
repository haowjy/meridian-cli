"""Sanity smoke tests — critical command surface.

Replaces: tests/e2e/quick-sanity.md
"""

import re


def test_help_exits_zero_and_contains_meridian(cli):
    """meridian --help exits 0, stdout contains expected commands."""
    result = cli("--help")
    result.assert_success()
    assert "meridian" in result.stdout.lower() or "spawn" in result.stdout


def test_help_exposes_core_commands(cli):
    """Help output mentions spawn, models, work."""
    result = cli("--help")
    result.assert_success()
    for cmd in ("spawn", "models", "work"):
        assert cmd in result.stdout, f"help missing {cmd}"


def test_version_exits_zero(cli):
    """meridian --version exits 0, outputs version string."""
    result = cli("--version")
    result.assert_success()
    # Version should contain digits and dots
    assert re.search(r"\d+\.\d+", result.stdout), f"version malformed: {result.stdout}"


def test_config_show_exits_zero(cli):
    """meridian config show exits 0 in an initialized repo."""
    result = cli("config", "show")
    result.assert_success()
    assert "defaults.model" in result.stdout or "model" in result.stdout


def test_models_list_redirects_to_mars(cli):
    """meridian models list is a nonzero compatibility stub."""
    result = cli("models", "list")
    result.assert_failure(1)
    assert "meridian mars models list" in result.stderr
    assert not result.stdout.strip()


def test_doctor_exits_zero(cli):
    """meridian doctor exits 0."""
    result = cli("doctor", "--prune")
    result.assert_success()


def test_doctor_help_exposes_prune_global_and_kill_orphans_flags(cli):
    """meridian doctor --help mentions prune, global, and kill-orphans flags."""
    result = cli("doctor", "--help")
    result.assert_success()
    assert "--prune" in result.stdout
    assert "--global" in result.stdout
    assert "--kill-orphans" in result.stdout


def test_spawn_list_exits_zero(cli, scratch_dir):
    """meridian spawn list exits 0 with empty list."""
    # Create minimal agent for spawn list to work
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "test.md").write_text("# Test Agent\n", encoding="utf-8")

    result = cli("spawn", "list", json_mode=True)
    result.assert_success()
    data = result.json
    assert "spawns" in data
    assert isinstance(data["spawns"], list)


def test_unknown_command_exits_nonzero(cli):
    """Unknown subcommand exits non-zero."""
    result = cli("nonexistent-command-xyz")
    result.assert_failure()


def test_spawn_dry_run_exits_zero(cli, scratch_dir):
    """meridian spawn --dry-run exits 0."""
    # Create minimal agent
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")

    result = cli(
        "spawn", "-a", "reviewer", "-p", "test prompt",
        "--dry-run", json_mode=True
    )
    result.assert_success()
    data = result.json
    assert data.get("status") == "dry-run"
