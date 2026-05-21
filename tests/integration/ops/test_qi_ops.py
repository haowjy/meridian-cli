"""High-leverage inline knowledge navigation integration tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from meridian.lib.ops.qi import discover_knowledge_points, qi_check_sync, qi_show_sync


def _make_agents_md(directory: Path, *, body: str = "# Agents\n") -> Path:
    f = directory / "AGENTS.md"
    f.write_text(body, encoding="utf-8")
    return f


def _make_context_md(directory: Path, *, body: str = "# Context\n") -> Path:
    ctx_dir = directory / ".context"
    ctx_dir.mkdir(exist_ok=True)
    f = ctx_dir / "CONTEXT.md"
    f.write_text(body, encoding="utf-8")
    return f


def test_discovery_skips_generated_and_dependency_dirs(tmp_path: Path) -> None:
    for skipped in [".git", "__pycache__", "node_modules", ".venv", "dist", ".agents"]:
        skip_dir = tmp_path / skipped
        skip_dir.mkdir()
        _make_agents_md(skip_dir)

    assert discover_knowledge_points(tmp_path) == []


def test_discovery_paths_are_project_relative_with_forward_slashes(tmp_path: Path) -> None:
    sub = tmp_path / "docs"
    sub.mkdir()
    _make_agents_md(sub)

    points = discover_knowledge_points(tmp_path)

    assert [point.rel_path for point in points] == ["docs/AGENTS.md"]


def test_qi_show_boundary_path_is_project_relative(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    _make_agents_md(sub)

    result = qi_show_sync(sub, tmp_path)

    assert result.boundary_path == "sub"
    assert not result.boundary_path.startswith("/")


def test_missing_context_dir_without_context_md_is_error(tmp_path: Path) -> None:
    (tmp_path / ".context").mkdir()

    result = qi_check_sync(tmp_path)

    findings = [finding for finding in result.findings if finding.category == "missing_context_md"]
    assert findings and findings[0].severity == "error"


def test_orphan_context_without_sibling_agents_is_warning(tmp_path: Path) -> None:
    _make_context_md(tmp_path)

    result = qi_check_sync(tmp_path)

    findings = [finding for finding in result.findings if finding.category == "orphan_context"]
    assert findings and findings[0].severity == "warning"


def test_sibling_context_link_missing_file_is_missing_agents_warning(tmp_path: Path) -> None:
    _make_agents_md(tmp_path, body="# Agents\n\nSee [context](.context/CONTEXT.md).\n")

    result = qi_check_sync(tmp_path)

    findings = [finding for finding in result.findings if finding.category == "missing_agents"]
    assert findings and findings[0].severity == "warning"


def test_non_sibling_context_link_is_broken_link_not_missing_agents(tmp_path: Path) -> None:
    other = tmp_path / "shared"
    other.mkdir()
    _make_agents_md(tmp_path, body="# Agents\n\nSee [ctx](shared/.context/CONTEXT.md).\n")

    result = qi_check_sync(tmp_path)

    categories = [finding.category for finding in result.findings]
    assert "broken_link" in categories
    assert "missing_agents" not in categories


def test_broken_link_in_agents_md_is_error(tmp_path: Path) -> None:
    _make_agents_md(tmp_path, body="# Agents\n\nSee [missing](docs/missing.md).\n")

    result = qi_check_sync(tmp_path)

    findings = [finding for finding in result.findings if finding.category == "broken_link"]
    assert findings and findings[0].severity == "error"


def test_broken_link_in_context_md_is_error(tmp_path: Path) -> None:
    _make_agents_md(tmp_path)
    _make_context_md(tmp_path, body="# Context\n\nSee [missing](../missing.md).\n")

    result = qi_check_sync(tmp_path)

    findings = [finding for finding in result.findings if finding.category == "broken_link"]
    assert findings and findings[0].severity == "error"


@pytest.mark.skipif(sys.platform == "win32", reason="chmod not reliable on Windows")
def test_unreadable_file_produces_error_finding(tmp_path: Path) -> None:
    agents_file = _make_agents_md(tmp_path)
    agents_file.chmod(0o000)
    try:
        result = qi_check_sync(tmp_path)
    finally:
        agents_file.chmod(0o644)

    findings = [finding for finding in result.findings if finding.category == "unreadable"]
    assert findings and findings[0].severity == "error"
