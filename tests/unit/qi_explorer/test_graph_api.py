"""Unit tests for qi explore graph collapse."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.kg.graph import build_analysis
from meridian.qi_explorer.discovery import DiscoveryResult, ScanRoot
from meridian.qi_explorer.graph_api import (
    analysis_to_graph,
    build_graph_index,
    qi_file_filter,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_boundary_collapse_and_cross_ref_edge(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    alpha = root / "alpha"
    beta = root / "beta"
    _write(alpha / "AGENTS.md", "# Alpha\n\nSee [beta](../beta/AGENTS.md).\n")
    _write(beta / "AGENTS.md", "# Beta\n")

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
    index = build_graph_index(analysis, discovery)
    graph = analysis_to_graph(analysis, discovery, index=index)

    assert graph["nodeCount"] == 2
    assert graph["edgeCount"] == 1
    assert {node["id"] for node in graph["nodes"]} == {"codebase:alpha", "codebase:beta"}
    link = graph["links"][0]
    assert link["source"] == "codebase:alpha"
    assert link["target"] == "codebase:beta"
    assert link["weight"] == 1
    assert index.inbound_from["codebase:beta"] == ["codebase:alpha"]


def test_agents_and_context_collapse_to_one_node(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    module = root / "module"
    _write(module / "AGENTS.md", "# Module Agents\n")
    _write(module / ".context" / "CONTEXT.md", "# Module Context\n")

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
    graph = analysis_to_graph(analysis, discovery)

    assert graph["nodeCount"] == 1
    node = graph["nodes"][0]
    assert node["id"] == "codebase:module"
    assert node["label"] == "Module Agents"
    assert node["has"] == {"agents": True, "context": True}


def test_multi_root_namespacing(tmp_path: Path) -> None:
    codebase = tmp_path / "codebase"
    kb = tmp_path / "kb"
    _write(codebase / "AGENTS.md", "# Codebase\n")
    _write(kb / "AGENTS.md", "# Knowledge Base\n")

    discovery = DiscoveryResult(
        primary=codebase.resolve(),
        roots=[
            ScanRoot(name="codebase", abs_path=codebase.resolve(), kind="primary"),
            ScanRoot(name="kb", abs_path=kb.resolve(), kind="context"),
        ],
    )
    analysis = build_analysis(
        root=codebase,
        roots=[codebase, kb],
        file_filter=qi_file_filter,
        include_backlinks=False,
        include_clusters=False,
    )
    graph = analysis_to_graph(analysis, discovery)

    ids = {node["id"] for node in graph["nodes"]}
    groups = {node["group"] for node in graph["nodes"]}
    assert ids == {"codebase:.", "kb:."}
    assert groups == {"codebase", "kb"}
    kb_node = next(node for node in graph["nodes"] if node["scanRoot"] == "kb")
    assert kb_node["scanRootKind"] == "context"
    assert kb_node["label"] == "Knowledge Base"
