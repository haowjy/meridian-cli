"""KG graph analysis seam tests — file_filter, multi-root, SKIP_DIRS."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.ignores import SKIP_DIRS
from meridian.lib.kg.graph import build_analysis


def _write_md(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _qi_file_filter(path: Path) -> bool:
    return path.name == "AGENTS.md" or path.as_posix().endswith(".context/CONTEXT.md")


def test_file_filter_narrows_nodes_to_matching_files(tmp_path: Path) -> None:
    _write_md(tmp_path / "AGENTS.md", "# Agents\n")
    _write_md(tmp_path / "README.md", "# Readme\n")
    _write_md(tmp_path / "docs" / "guide.md", "# Guide\n")

    result = build_analysis(tmp_path, file_filter=_qi_file_filter)

    assert set(result.nodes) == {tmp_path / "AGENTS.md"}


def test_skip_dirs_prunes_meridian_subdir_from_kg_scan(tmp_path: Path) -> None:
    _write_md(tmp_path / "visible.md", "# Visible\n")
    meridian_dir = tmp_path / ".meridian"
    meridian_dir.mkdir()
    _write_md(meridian_dir / "hidden.md", "# Hidden\n")

    assert ".meridian" in SKIP_DIRS

    result = build_analysis(tmp_path)

    assert set(result.nodes) == {tmp_path / "visible.md"}


def test_multi_root_merges_nodes_and_resolves_cross_root_edges(tmp_path: Path) -> None:
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    root_a.mkdir()
    root_b.mkdir()

    doc_a = _write_md(
        root_a / "docs" / "a.md",
        "# A\n\nSee [B](../../repo-b/docs/b.md).\n",
    )
    doc_b = _write_md(root_b / "docs" / "b.md", "# B\n")

    result = build_analysis(
        tmp_path,
        roots=[root_a, root_b],
        include_backlinks=False,
        include_clusters=False,
    )

    assert set(result.nodes) == {doc_a, doc_b}

    node_a = result.nodes[doc_a]
    node_b = result.nodes[doc_b]

    assert node_a.scan_root == root_a.resolve()
    assert node_b.scan_root == root_b.resolve()
    assert node_a.rel_path == "docs/a.md"
    assert node_b.rel_path == "docs/b.md"

    resolved_edges = [
        edge for edge in result.edges if edge.resolved and isinstance(edge.dst, Path)
    ]
    cross_root = [
        edge
        for edge in resolved_edges
        if edge.src == doc_a and edge.dst == doc_b
    ]
    assert cross_root, "expected cross-root link from A to B to resolve"


def test_nested_roots_attribute_to_longest_matching_root(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)

    nested_doc = _write_md(inner / "nested.md", "# Nested\n")

    result = build_analysis(
        tmp_path,
        roots=[outer, inner],
        include_backlinks=False,
        include_clusters=False,
    )

    node = result.nodes[nested_doc]
    assert node.scan_root == inner.resolve()
    assert node.rel_path == "nested.md"
