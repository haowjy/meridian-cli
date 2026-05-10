"""Unit tests for meridian.lib.ops.qi — inline knowledge navigation."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.ops.qi import (
    QiKnowledgePoint,
    QiListOutput,
    QiShowOutput,
    discover_knowledge_points,
    find_boundary,
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
