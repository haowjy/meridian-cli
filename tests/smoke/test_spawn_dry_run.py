"""Spawn dry-run smoke tests — prompt assembly without harness invocation.

Replaces: tests/e2e/spawn/dry-run.md
"""


def test_basic_dry_run(cli, scratch_dir):
    """Basic dry-run outputs launch spec with composed prompt."""
    # Create minimal agent
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")

    result = cli(
        "spawn", "-a", "reviewer", "-p", "Write hello world",
        "--dry-run", json_mode=True
    )
    result.assert_success()
    data = result.json
    assert data["status"] == "dry-run"
    assert "Write hello world" in data["composed_prompt"]
    assert "model" in data
    assert data["terminal_surface_mode"] == "pty_mediated"


def test_model_override(cli, scratch_dir):
    """Model override is accepted in dry-run."""
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "reviewer.md").write_text(
        "---\nname: reviewer\nmodel: gpt-5.4\n---\n# Reviewer\n",
        encoding="utf-8",
    )
    # Seed mars.toml so model resolution works
    mars_content = "[settings]\nmodels_cache_ttl_hours = 24\n"
    (scratch_dir / "mars.toml").write_text(mars_content, encoding="utf-8")

    result = cli(
        "spawn", "-a", "reviewer", "-p", "test", "-m", "gpt-5.5",
        "--dry-run", json_mode=True
    )
    result.assert_success()
    data = result.json
    assert data["status"] == "dry-run"
    assert data["model"] == "gpt-5.5"
    assert data["model_selection"]["requested_token"] == "gpt-5.5"
    assert data["model_selection"]["canonical_model_id"] == "gpt-5.5"
    assert data["harness_id"] == "codex"
    assert data["terminal_surface_mode"] == "pty_mediated"


def test_agent_overlay_model_precedence_visible_in_dry_run(cli, scratch_dir):
    """Dry-run reflects project-local agent model overlays and CLI model override."""
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "reviewer.md").write_text(
        "---\nname: reviewer\nmodel: gpt-5.4\n---\n# Reviewer\n",
        encoding="utf-8",
    )
    (scratch_dir / "mars.toml").write_text(
        "[settings]\nmodels_cache_ttl_hours = 24\n",
        encoding="utf-8",
    )
    (scratch_dir / "meridian.toml").write_text(
        '[agents.reviewer]\nmodel = "gpt-5.4"\n',
        encoding="utf-8",
    )
    (scratch_dir / "meridian.local.toml").write_text(
        '[agents.reviewer]\nmodel = "gpt-5.5"\n',
        encoding="utf-8",
    )

    overlay_result = cli(
        "spawn", "-a", "reviewer", "-p", "test",
        "--dry-run", json_mode=True
    )
    overlay_result.assert_success()
    overlay_data = overlay_result.json
    assert overlay_data["status"] == "dry-run"
    assert overlay_data["model"] == "gpt-5.5"
    assert overlay_data["model_selection"]["requested_token"] == "gpt-5.5"
    assert overlay_data["model_selection"]["canonical_model_id"] == "gpt-5.5"
    assert overlay_data["harness_id"] == "codex"
    assert overlay_data["terminal_surface_mode"] == "pty_mediated"

    cli_override_result = cli(
        "spawn", "-a", "reviewer", "-p", "test",
        "-m", "gpt-5.4",
        "--dry-run", json_mode=True
    )
    cli_override_result.assert_success()
    cli_override_data = cli_override_result.json
    assert cli_override_data["status"] == "dry-run"
    assert cli_override_data["model"] == "gpt-5.4"
    assert cli_override_data["model_selection"]["requested_token"] == "gpt-5.4"
    assert cli_override_data["model_selection"]["canonical_model_id"] == "gpt-5.4"
    assert cli_override_data["harness_id"] == "codex"
    assert cli_override_data["terminal_surface_mode"] == "pty_mediated"


def test_template_vars_substitution(cli, scratch_dir):
    """Template vars are substituted in prompt."""
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")

    result = cli(
        "spawn", "-a", "reviewer",
        "-p", "Review {{FILE_PATH}} for {{CONCERN}}",
        "--prompt-var", "FILE_PATH=src/main.py",
        "--prompt-var", "CONCERN=security",
        "--dry-run",
        json_mode=True
    )
    result.assert_success()
    data = result.json
    prompt = data["composed_prompt"]
    assert "src/main.py" in prompt
    assert "security" in prompt
    assert "{{FILE_PATH}}" not in prompt
    assert "{{CONCERN}}" not in prompt


def test_reference_files_attached(cli, scratch_dir, tmp_path):
    """Reference files are included in dry-run payload."""
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")

    # Create a reference file
    ref_file = tmp_path / "ref.md"
    ref_file.write_text("# Reference\n", encoding="utf-8")

    result = cli(
        "spawn", "-a", "reviewer",
        "-p", "Review this file",
        "-f", str(ref_file),
        "--dry-run",
        json_mode=True
    )
    result.assert_success()
    data = result.json
    # Reference files should be in the payload
    refs = data.get("reference_files", [])
    assert refs or "ref.md" in str(data)  # Either explicit refs or mentioned


def test_empty_prompt_handled_gracefully(cli, scratch_dir):
    """Empty prompt fails or succeeds cleanly without traceback."""
    agents_dir = scratch_dir / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")

    result = cli(
        "spawn", "-a", "reviewer", "-p", "", "--dry-run", json_mode=True
    )
    # May succeed or fail, but should not traceback
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_worktree_like_cwd_uses_env_project_root_authority(tmp_path):
    """MERIDIAN_PROJECT_DIR intentionally wins over a worktree-like cwd boundary."""
    import subprocess

    from tests.smoke.conftest import _cli_cmd, _isolated_env

    canonical_root = tmp_path / "canonical-root"
    worktree_cwd = tmp_path / "worktrees" / "arch-exec"
    agents_dir = canonical_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    worktree_cwd.mkdir(parents=True, exist_ok=True)
    (canonical_root / "mars.toml").write_text(
        "[settings]\ntargets = [\".claude\"]\n",
        encoding="utf-8",
    )
    (agents_dir / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")
    (worktree_cwd / ".git").write_text("gitdir: /tmp/fake-worktree-git\n", encoding="utf-8")

    env = _isolated_env()
    env["MERIDIAN_PROJECT_DIR"] = str(canonical_root)
    env["MERIDIAN_HOME"] = str(tmp_path / "home")

    proc = subprocess.run(
        _cli_cmd("spawn", "-a", "reviewer", "-p", "test", "--dry-run", json_mode=True),
        cwd=str(worktree_cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    data = __import__("json").loads(proc.stdout)
    assert data["resolved_authority"]["project_root"] == canonical_root.resolve().as_posix()
    assert data["resolved_authority"]["project_root_source"] == "explicit"
