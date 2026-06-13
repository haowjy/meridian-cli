"""Unit tests for qi explore file serving and link security."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.kg.graph import build_analysis
from meridian.qi_explorer.content import serve_file
from meridian.qi_explorer.discovery import DiscoveryResult, ScanRoot
from meridian.qi_explorer.graph_api import GraphIndex, build_graph_index, qi_file_filter


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _index_for_root(root: Path) -> GraphIndex:
    discovery = DiscoveryResult(
        primary=root.resolve(),
        roots=[ScanRoot(name="codebase", abs_path=root.resolve(), kind="primary")],
    )
    analysis = build_analysis(
        root=root,
        roots=[root],
        file_filter=qi_file_filter,
        include_backlinks=False,
        include_clusters=False,
    )
    return build_graph_index(analysis, discovery)


def test_path_traversal_is_forbidden(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root / "AGENTS.md", "# Root\n")
    index = _index_for_root(root)

    result = serve_file("codebase:../../../etc/passwd", index)

    assert result.forbidden is True
    assert result.kind == "not-found"


def test_markdown_and_source_kinds(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root / "AGENTS.md", "# Root\n")
    _write(root / "notes.md", "# Notes\n")
    _write(root / "plain.txt", "hello")
    index = _index_for_root(root)

    markdown = serve_file("codebase:notes.md", index)
    source = serve_file("codebase:plain.txt", index)

    assert markdown.kind == "markdown"
    assert "<h1>" in markdown.content
    assert source.kind == "source"
    assert "hello" in source.content


def test_binary_detection(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root / "AGENTS.md", "# Root\n")
    binary = root / "blob.bin"
    binary.write_bytes(b"hello\x00world")
    index = _index_for_root(root)

    result = serve_file("codebase:blob.bin", index)

    assert result.kind == "binary"
    assert result.content == "(binary file)"
