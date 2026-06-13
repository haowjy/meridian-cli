"""Unit tests for qi explore graph collapse."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.kg.graph import build_analysis
from meridian.qi_explorer.discovery import DiscoveryResult, ScanRoot, discover_scan_roots
from meridian.qi_explorer.graph_api import (
    analysis_to_graph,
    build_graph_index,
    qi_file_filter,
)

if TYPE_CHECKING:
    import pytest


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


def test_directory_link_creates_cross_ref_edge(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    alpha = root / "alpha"
    beta = root / "beta"
    _write(alpha / "AGENTS.md", "# Alpha\n\nSee [beta dir](../beta/).\n")
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

    assert graph["edgeCount"] == 1
    link = graph["links"][0]
    assert link["source"] == "codebase:alpha"
    assert link["target"] == "codebase:beta"
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


def test_multi_root_namespacing_with_shared_boundary_path(tmp_path: Path) -> None:
    codebase = tmp_path / "codebase"
    kb = tmp_path / "kb"
    _write(
        codebase / "shared" / "AGENTS.md",
        "# Codebase Shared\n\nSee [kb](../../kb/shared/AGENTS.md).\n",
    )
    _write(kb / "shared" / "AGENTS.md", "# KB Shared\n")

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
    assert ids == {"codebase:shared", "kb:shared"}
    assert graph["edgeCount"] == 1
    link = graph["links"][0]
    assert link["source"] == "codebase:shared"
    assert link["target"] == "kb:shared"


def test_discover_scan_roots_ignores_inherited_meridian_project_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = tmp_path / "project-a"
    project_a_kb = project_a / "kb-a"
    project_b = tmp_path / "project-b"
    project_b_kb = project_b / "kb-b"
    nested = project_b / "src" / "nested"
    nested.mkdir(parents=True)
    project_a_kb.mkdir(parents=True)
    project_b_kb.mkdir(parents=True)

    _write(project_a / "meridian.toml", '[context.kb]\npath = "kb-a"\n')
    _write(project_b / "meridian.toml", '[context.kb]\npath = "kb-b"\n')
    _write(nested / "AGENTS.md", "# Nested\n")
    _write(project_b_kb / "AGENTS.md", "# KB B\n")
    _write(project_a_kb / "AGENTS.md", "# KB A\n")

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(project_a.resolve()))

    discovery = discover_scan_roots(nested)

    assert discovery.primary == nested.resolve()
    assert discovery.name_to_path["kb"] == project_b_kb.resolve()
    assert discovery.name_to_path["kb"] != project_a_kb.resolve()


def test_nested_subdir_discovers_same_context_roots(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    kb = project_root / "kb"
    nested = project_root / "src" / "nested"
    nested.mkdir(parents=True)
    kb.mkdir()
    _write(project_root / "meridian.toml", "[context.kb]\npath = \"kb\"\n")
    _write(project_root / "AGENTS.md", "# Root\n")
    _write(nested / "AGENTS.md", "# Nested\n")
    _write(kb / "AGENTS.md", "# KB\n")

    from_root = discover_scan_roots(project_root)
    from_nested = discover_scan_roots(nested)

    assert {root.name for root in from_root.roots} == {root.name for root in from_nested.roots}
    assert from_root.name_to_path["kb"] == from_nested.name_to_path["kb"]
    assert from_root.primary == project_root.resolve()
    assert from_nested.primary == nested.resolve()
