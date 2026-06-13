"""Unit tests for qi explore file serving and link security."""

from __future__ import annotations

import re
from pathlib import Path

from meridian.lib.kg.graph import build_analysis
from meridian.qi_explorer.content import (
    categorize_link,
    render_markdown_html,
    serve_file,
)
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
    assert source.content == "hello"
    assert "<pre>" not in source.content


def test_binary_detection(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root / "AGENTS.md", "# Root\n")
    binary = root / "blob.bin"
    binary.write_bytes(b"hello\x00world")
    index = _index_for_root(root)

    result = serve_file("codebase:blob.bin", index)

    assert result.kind == "binary"
    assert result.content == "(binary file)"


def test_xss_payload_is_escaped_in_agents_html(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(
        root / "AGENTS.md",
        "# Root\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n",
    )
    index = _index_for_root(root)
    node_id = next(iter(index.nodes_by_id))
    from meridian.qi_explorer.content import build_content_payload

    payload = build_content_payload(node_id, index)
    assert payload is not None
    agents_html = str(payload["agentsHtml"])
    assert "<script>" not in agents_html
    assert "<img" not in agents_html
    assert "&lt;script&gt;" in agents_html
    assert "&lt;img" in agents_html


def test_module_readme_link_is_source_ref_not_cross_ref(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root / "AGENTS.md", "# Root\n\nSee [module](module/README.md).\n")
    _write(root / "module" / "README.md", "# Module readme\n")
    index = _index_for_root(root)

    category, node_id, file_id = categorize_link(
        "module/README.md",
        source_file=root / "AGENTS.md",
        index=index,
    )

    assert category == "source-ref"
    assert node_id is None
    assert file_id == "codebase:module/README.md"


def test_anchor_annotation_attributes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    beta = root / "beta"
    _write(
        root / "AGENTS.md",
        "\n".join(
            [
                "# Root",
                "",
                "[cross](beta/AGENTS.md)",
                "[source](notes.md)",
                "[external](https://example.com)",
                "[broken](missing.md)",
            ]
        ),
    )
    _write(beta / "AGENTS.md", "# Beta\n")
    _write(root / "notes.md", "# Notes\n")
    index = _index_for_root(root)

    html_out = render_markdown_html(root / "AGENTS.md", index)

    def anchor_for(label: str) -> str:
        match = re.search(rf'<a[^>]*>{re.escape(label)}</a>', html_out)
        assert match is not None, html_out
        return match.group(0)

    cross = anchor_for("cross")
    source = anchor_for("source")
    external = anchor_for("external")
    broken = anchor_for("broken")

    for tag in (cross, source, external, broken):
        assert tag.count('class="') == 1
        assert tag.count("data-category=") == 1

    assert 'data-category="cross-ref"' in cross
    assert "data-node-id=" in cross
    assert 'data-category="source-ref"' in source
    assert "data-file=" in source
    assert 'data-category="external"' in external
    assert 'data-category="broken"' in broken
    assert "qi-link--broken" in broken
