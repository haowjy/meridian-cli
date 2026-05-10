"""Context query smoke tests — path resolution.

Fills CLI-level gap for context command.
"""

import json
from pathlib import Path


def _posix_str(p: Path | str) -> str:
    """Normalize path to forward slashes for CLI output comparison."""
    return str(p).replace("\\", "/")


def _write_strategy_context_config(scratch_dir):
    (scratch_dir / "meridian.toml").write_text(
        """
[context.strategy]
source = "git"
remote = "git@github.com:meridian-flow/docs.git"
path = "voluma-bio/strategy"
""".lstrip(),
        encoding="utf-8",
    )


def test_context_work_outputs_path_or_null(cli):
    """context work is empty when no active work item is attached."""
    result = cli("context", "work")
    result.assert_success()
    assert result.stdout.strip() == ""


def test_context_kb_outputs_path(cli, scratch_dir):
    """context kb resolves to the project-local KB path."""
    result = cli("context", "kb")
    result.assert_success()
    assert result.stdout.strip() == _posix_str(scratch_dir / ".meridian" / "kb")


def test_context_work_archive_outputs_path(cli, scratch_dir):
    """context work.archive resolves to the project-local work archive."""
    result = cli("context", "work.archive")
    result.assert_success()
    assert result.stdout.strip() == _posix_str(scratch_dir / ".meridian" / "archive" / "work")


def test_context_strategy_outputs_path(cli, scratch_dir):
    """context strategy outputs configured arbitrary context path."""
    _write_strategy_context_config(scratch_dir)
    result = cli("context", "strategy")
    result.assert_success()
    assert result.stdout.strip().endswith("/voluma-bio/strategy")


def test_context_verbose_shows_details(cli, scratch_dir):
    """context --verbose shows source/path details."""
    _write_strategy_context_config(scratch_dir)
    result = cli("context", "--verbose")
    result.assert_success()
    assert "strategy:" in result.stdout
    assert "path: voluma-bio/strategy" in result.stdout
    assert f"resolved: {_posix_str(scratch_dir / '.meridian' / 'kb')}" in result.stdout
    archive = _posix_str(scratch_dir / ".meridian" / "archive" / "work")
    assert f"archive_resolved: {archive}" in result.stdout


def test_context_json_format(cli, scratch_dir):
    """context with --json outputs valid JSON."""
    result = cli("context", json_mode=True)
    result.assert_success()
    data = json.loads(result.stdout)
    assert data["active_work_dir"] is None
    assert data["kb_source"] == "local"
    assert data["kb_resolved"] == _posix_str(scratch_dir / ".meridian" / "kb")
    assert data["work_resolved"] == _posix_str(scratch_dir / ".meridian" / "work")
    assert data["work_archive_resolved"] == _posix_str(
        scratch_dir / ".meridian" / "archive" / "work"
    )
