"""Unit tests for meridian.lib.ops.qi — inline knowledge navigation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from meridian.lib.ops.qi import (
    QiCheckFinding,
    QiCheckOutput,
    QiKnowledgePoint,
    QiListOutput,
    QiShowOutput,
    discover_knowledge_points,
    find_boundary,
    qi_check_sync,
    qi_list_sync,
    qi_show_sync,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agents_md(directory: Path) -> Path:
    f = directory / "AGENTS.md"
    f.write_text("# Agents\n", encoding="utf-8")
    return f


def _make_context_md(directory: Path) -> Path:
    ctx_dir = directory / ".context"
    ctx_dir.mkdir(exist_ok=True)
    f = ctx_dir / "CONTEXT.md"
    f.write_text("# Context\n", encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# discover_knowledge_points
# ---------------------------------------------------------------------------


class TestDiscoverKnowledgePoints:
    def test_finds_agents_md(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        points = discover_knowledge_points(tmp_path)
        assert any(p.kind == "agents" for p in points)

    def test_finds_context_md(self, tmp_path: Path) -> None:
        _make_context_md(tmp_path)
        points = discover_knowledge_points(tmp_path)
        assert any(p.kind == "context" for p in points)

    def test_finds_both_in_same_dir(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        _make_context_md(tmp_path)
        points = discover_knowledge_points(tmp_path)
        kinds = {p.kind for p in points}
        assert "agents" in kinds
        assert "context" in kinds

    def test_finds_nested(self, tmp_path: Path) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        _make_agents_md(sub)
        points = discover_knowledge_points(tmp_path)
        assert any(p.kind == "agents" for p in points)

    def test_empty_inventory(self, tmp_path: Path) -> None:
        points = discover_knowledge_points(tmp_path)
        assert points == []

    def test_skips_excluded_dirs(self, tmp_path: Path) -> None:
        for skipped in [".git", "__pycache__", "node_modules", ".venv", "dist", ".agents"]:
            skip_dir = tmp_path / skipped
            skip_dir.mkdir()
            _make_agents_md(skip_dir)
        points = discover_knowledge_points(tmp_path)
        assert points == []

    def test_paths_are_root_relative_forward_slashes(self, tmp_path: Path) -> None:
        sub = tmp_path / "docs"
        sub.mkdir()
        _make_agents_md(sub)
        points = discover_knowledge_points(tmp_path)
        assert points
        assert "\\" not in points[0].rel_path

    def test_returns_qi_knowledge_point_instances(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        points = discover_knowledge_points(tmp_path)
        assert all(isinstance(p, QiKnowledgePoint) for p in points)

    def test_context_md_not_found_without_subdir(self, tmp_path: Path) -> None:
        # A bare .context file (not a dir) should not produce a context point.
        (tmp_path / ".context").write_text("not a dir", encoding="utf-8")
        points = discover_knowledge_points(tmp_path)
        assert not any(p.kind == "context" for p in points)


# ---------------------------------------------------------------------------
# find_boundary
# ---------------------------------------------------------------------------


class TestFindBoundary:
    def test_finds_agents_md_in_same_dir(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        boundary = find_boundary(tmp_path)
        assert boundary == tmp_path

    def test_finds_context_md_in_same_dir(self, tmp_path: Path) -> None:
        _make_context_md(tmp_path)
        boundary = find_boundary(tmp_path)
        assert boundary == tmp_path

    def test_walks_up_to_parent(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        boundary = find_boundary(nested)
        assert boundary == tmp_path

    def test_returns_none_when_no_boundary(self, tmp_path: Path) -> None:
        # tmp_path itself has no AGENTS.md, and ancestors won't either
        # (we just need a plain directory with no knowledge files).
        # Use a subdirectory that has no boundary up to tmp_path.
        boundary = find_boundary(tmp_path)
        assert boundary is None

    def test_accepts_file_path(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        some_file = tmp_path / "README.md"
        some_file.write_text("hi", encoding="utf-8")
        boundary = find_boundary(some_file)
        assert boundary == tmp_path

    def test_prefers_nearest_boundary(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        child = tmp_path / "child"
        child.mkdir()
        _make_agents_md(child)
        boundary = find_boundary(child)
        assert boundary == child


# ---------------------------------------------------------------------------
# qi_list_sync
# ---------------------------------------------------------------------------


class TestQiListSync:
    def test_returns_qi_list_output(self, tmp_path: Path) -> None:
        result = qi_list_sync(tmp_path)
        assert isinstance(result, QiListOutput)

    def test_empty_points(self, tmp_path: Path) -> None:
        result = qi_list_sync(tmp_path)
        assert result.points == []

    def test_format_text_empty(self, tmp_path: Path) -> None:
        result = qi_list_sync(tmp_path)
        text = result.format_text()
        assert "No inline knowledge" in text

    def test_format_text_with_points(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        result = qi_list_sync(tmp_path)
        text = result.format_text()
        assert "[agents]" in text
        assert "AGENTS.md" in text


# ---------------------------------------------------------------------------
# qi_show_sync
# ---------------------------------------------------------------------------


class TestQiShowSync:
    def test_returns_qi_show_output(self, tmp_path: Path) -> None:
        result = qi_show_sync(tmp_path, tmp_path)
        assert isinstance(result, QiShowOutput)

    def test_agents_content_loaded(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        result = qi_show_sync(tmp_path, tmp_path)
        assert result.agents_content is not None
        assert "Agents" in result.agents_content

    def test_context_content_loaded(self, tmp_path: Path) -> None:
        _make_context_md(tmp_path)
        result = qi_show_sync(tmp_path, tmp_path)
        assert result.context_content is not None
        assert "Context" in result.context_content

    def test_agents_only_boundary(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        result = qi_show_sync(tmp_path, tmp_path)
        assert result.agents_content is not None
        assert result.context_content is None

    def test_no_boundary_gives_no_content(self, tmp_path: Path) -> None:
        # No AGENTS.md, no .context/CONTEXT.md — content should be None.
        result = qi_show_sync(tmp_path, tmp_path)
        assert result.agents_content is None
        assert result.context_content is None

    def test_format_text_no_content(self, tmp_path: Path) -> None:
        result = qi_show_sync(tmp_path, tmp_path)
        text = result.format_text()
        assert "No inline knowledge" in text

    def test_format_text_with_agents(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        result = qi_show_sync(tmp_path, tmp_path)
        text = result.format_text()
        assert "AGENTS.md" in text
        assert "Agents" in text

    def test_boundary_path_is_relative_when_inside_project(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        _make_agents_md(sub)
        result = qi_show_sync(sub, tmp_path)
        # boundary_path should be relative to project root, not absolute
        assert not result.boundary_path.startswith("/")
        assert result.boundary_path == "sub"

    def test_walks_up_from_nested_file(self, tmp_path: Path) -> None:
        _make_agents_md(tmp_path)
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        a_file = deep / "something.py"
        a_file.write_text("x = 1", encoding="utf-8")
        result = qi_show_sync(a_file, tmp_path)
        assert result.agents_content is not None


# ---------------------------------------------------------------------------
# qi_check_sync
# ---------------------------------------------------------------------------


class TestQiCheckSync:
    def test_returns_qi_check_output(self, tmp_path: Path) -> None:
        result = qi_check_sync(tmp_path)
        assert isinstance(result, QiCheckOutput)

    def test_empty_project_passes_cleanly(self, tmp_path: Path) -> None:
        result = qi_check_sync(tmp_path)
        assert result.findings == []
        assert not result.has_errors
        assert result.points_scanned == 0

    def test_agents_only_passes_cleanly(self, tmp_path: Path) -> None:
        # AGENTS.md without .context/ is valid — incremental adoption is fine
        _make_agents_md(tmp_path)
        result = qi_check_sync(tmp_path)
        assert result.findings == []
        assert not result.has_errors

    def test_missing_context_md_in_context_dir(self, tmp_path: Path) -> None:
        # .context/ directory exists but has no CONTEXT.md → error
        (tmp_path / ".context").mkdir()
        result = qi_check_sync(tmp_path)
        cats = [f.category for f in result.findings]
        assert "missing_context_md" in cats
        errors = [f for f in result.findings if f.category == "missing_context_md"]
        assert errors[0].severity == "error"

    def test_orphan_context_no_sibling_agents(self, tmp_path: Path) -> None:
        # .context/CONTEXT.md without a sibling AGENTS.md → warning
        _make_context_md(tmp_path)
        result = qi_check_sync(tmp_path)
        cats = [f.category for f in result.findings]
        assert "orphan_context" in cats
        warns = [f for f in result.findings if f.category == "orphan_context"]
        assert warns[0].severity == "warning"

    def test_no_orphan_when_agents_present(self, tmp_path: Path) -> None:
        # Both AGENTS.md and .context/CONTEXT.md present → no orphan warning
        _make_agents_md(tmp_path)
        _make_context_md(tmp_path)
        result = qi_check_sync(tmp_path)
        orphans = [f for f in result.findings if f.category == "orphan_context"]
        assert orphans == []

    def test_missing_agents_for_broken_context_link(self, tmp_path: Path) -> None:
        # AGENTS.md explicitly links to .context/CONTEXT.md but file doesn't exist
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text(
            "# Agents\n\nSee [context](.context/CONTEXT.md) for context.\n",
            encoding="utf-8",
        )
        result = qi_check_sync(tmp_path)
        cats = [f.category for f in result.findings]
        assert "missing_agents" in cats
        warns = [f for f in result.findings if f.category == "missing_agents"]
        assert warns[0].severity == "warning"

    def test_broken_link_in_agents_md(self, tmp_path: Path) -> None:
        # AGENTS.md links to a file that doesn't exist → error
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text(
            "# Agents\n\nSee [missing](docs/missing.md) for details.\n",
            encoding="utf-8",
        )
        result = qi_check_sync(tmp_path)
        cats = [f.category for f in result.findings]
        assert "broken_link" in cats
        errors = [f for f in result.findings if f.category == "broken_link"]
        assert errors[0].severity == "error"

    def test_broken_link_in_context_md(self, tmp_path: Path) -> None:
        # .context/CONTEXT.md links to a file that doesn't exist → error
        _make_agents_md(tmp_path)
        ctx_dir = tmp_path / ".context"
        ctx_dir.mkdir()
        context_file = ctx_dir / "CONTEXT.md"
        context_file.write_text(
            "# Context\n\nSee [missing](../missing.md) for details.\n",
            encoding="utf-8",
        )
        result = qi_check_sync(tmp_path)
        cats = [f.category for f in result.findings]
        assert "broken_link" in cats

    def test_valid_link_in_agents_md_no_finding(self, tmp_path: Path) -> None:
        # AGENTS.md links to a file that exists → no broken_link finding
        readme = tmp_path / "README.md"
        readme.write_text("# Readme\n", encoding="utf-8")
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text(
            "# Agents\n\nSee [readme](README.md) for details.\n",
            encoding="utf-8",
        )
        result = qi_check_sync(tmp_path)
        broken = [f for f in result.findings if f.category == "broken_link"]
        assert broken == []

    def test_has_errors_property(self, tmp_path: Path) -> None:
        (tmp_path / ".context").mkdir()  # missing CONTEXT.md → error
        result = qi_check_sync(tmp_path)
        assert result.has_errors is True

    def test_error_and_warning_counts(self, tmp_path: Path) -> None:
        # Setup: .context/ without CONTEXT.md (error) + orphan in nested dir (warning)
        (tmp_path / ".context").mkdir()  # error: missing_context_md
        sub = tmp_path / "sub"
        sub.mkdir()
        _make_context_md(sub)  # warning: orphan_context (no AGENTS.md sibling)
        result = qi_check_sync(tmp_path)
        assert result.error_count >= 1
        assert result.warning_count >= 1

    def test_format_text_clean(self, tmp_path: Path) -> None:
        result = qi_check_sync(tmp_path)
        text = result.format_text()
        assert "All checks passed" in text

    def test_format_text_with_findings(self, tmp_path: Path) -> None:
        (tmp_path / ".context").mkdir()
        result = qi_check_sync(tmp_path)
        text = result.format_text()
        assert "error(s)" in text

    def test_findings_sorted_errors_before_warnings(self, tmp_path: Path) -> None:
        # .context/ dir without CONTEXT.md → error, orphan context in sub → warning
        (tmp_path / ".context").mkdir()
        sub = tmp_path / "sub"
        sub.mkdir()
        _make_context_md(sub)
        result = qi_check_sync(tmp_path)
        if len(result.findings) >= 2:
            severities = [f.severity for f in result.findings]
            # errors should come before warnings
            first_warn = next(
                (i for i, s in enumerate(severities) if s == "warning"), None
            )
            last_error = next(
                (
                    len(severities) - 1 - i
                    for i, s in enumerate(reversed(severities))
                    if s == "error"
                ),
                None,
            )
            if first_warn is not None and last_error is not None:
                assert last_error <= first_warn

    def test_findings_are_qi_check_finding_instances(self, tmp_path: Path) -> None:
        (tmp_path / ".context").mkdir()
        result = qi_check_sync(tmp_path)
        assert all(isinstance(f, QiCheckFinding) for f in result.findings)

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not reliable on Windows")
    def test_unreadable_file_produces_finding(self, tmp_path: Path) -> None:
        agents_file = _make_agents_md(tmp_path)
        agents_file.chmod(0o000)
        try:
            result = qi_check_sync(tmp_path)
            cats = [f.category for f in result.findings]
            assert "unreadable" in cats
            unreadable = [f for f in result.findings if f.category == "unreadable"]
            assert unreadable[0].severity == "error"
        finally:
            agents_file.chmod(0o644)
