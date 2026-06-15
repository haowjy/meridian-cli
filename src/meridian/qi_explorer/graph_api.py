"""Transform KG analysis into the qi explore API graph shape."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from meridian.lib.kg.graph import build_analysis
from meridian.lib.kg.types import AnalysisResult, GraphEdge, GraphNode
from meridian.lib.markdown.extract import extract_file
from meridian.qi_explorer.discovery import DiscoveryResult, ScanRoot

_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")
_AGENTS_NAME = "AGENTS.md"
_CONTEXT_SUFFIX = ".context/CONTEXT.md"

LinkCategory = Literal["cross-ref", "source-ref", "broken"]


def qi_file_filter(path: Path) -> bool:
    """Return True for qi-layer markdown files."""

    return path.name == _AGENTS_NAME or path.as_posix().endswith(_CONTEXT_SUFFIX)


def boundary_for_qi_doc(file_path: Path) -> Path:
    """Return the qi-layer boundary directory for a qi doc path."""

    if file_path.name == _AGENTS_NAME:
        return file_path.parent
    return file_path.parent.parent


# Backward-compatible alias for internal graph building.
boundary_dir = boundary_for_qi_doc


def _rel_boundary(boundary_dir: Path, scan_root: Path) -> str:
    rel = boundary_dir.resolve().relative_to(scan_root.resolve())
    if rel == Path("."):
        return "."
    return rel.as_posix()


def _first_h1(doc_path: Path | None) -> str | None:
    if doc_path is None:
        return None
    doc = extract_file(doc_path)
    if doc.error:
        return None
    for heading in doc.headings:
        if heading.level == 1 and heading.text.strip():
            return heading.text.strip()
    return None


def _node_label(
    *,
    agents_path: Path | None,
    context_path: Path | None,
    rel_path: str,
    root_name: str,
) -> str:
    label = _first_h1(agents_path)
    if label:
        return label
    label = _first_h1(context_path)
    if label:
        return label
    if rel_path == ".":
        return root_name
    return Path(rel_path).name


def _find_scan_root(path: Path, root_entries: list[tuple[str, Path]]) -> tuple[str, Path] | None:
    resolved = path.resolve()
    best: tuple[str, Path] | None = None
    for name, root in root_entries:
        root_resolved = root.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            continue
        if best is None or len(root_resolved.parts) > len(best[1].parts):
            best = (name, root_resolved)
    return best


def _scan_root_rel_id(name: str, path: Path, root: Path) -> str:
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    return f"{name}:{rel}"


@dataclass(frozen=True)
class ResolvedLocalTarget:
    """Classification of a resolved local link target."""

    category: LinkCategory
    node_id: str | None = None
    file_id: str | None = None


def resolve_local_target(resolved: Path, index: GraphIndex) -> ResolvedLocalTarget:
    """Classify a resolved local path for graph edges and HTML annotation.

  Returns ``cross-ref`` when the target is (a) a qi doc already indexed on a
  node, or (b) a boundary directory present in ``boundary_key_to_id``.
  Any other existing file inside a scan root is ``source-ref``.
    """

    target = resolved.resolve()

    if target.is_file():
        node_id = index.file_to_node_id.get(target)
        if node_id is not None:
            return ResolvedLocalTarget("cross-ref", node_id=node_id)

    if target.is_dir():
        root_match = _find_scan_root(target, index.root_entries)
        if root_match is not None:
            _, root = root_match
            node_id = index.boundary_key_to_id.get((root, target))
            if node_id is not None:
                return ResolvedLocalTarget("cross-ref", node_id=node_id)

    root_match = _find_scan_root(target, index.root_entries)
    if root_match is not None:
        name, root = root_match
        if target.exists() and target.is_file():
            return ResolvedLocalTarget(
                "source-ref",
                file_id=_scan_root_rel_id(name, target, root),
            )
        return ResolvedLocalTarget("broken")

    if target.exists() and target.is_file():
        return ResolvedLocalTarget("source-ref")
    return ResolvedLocalTarget("broken")


@dataclass(frozen=True)
class BoundaryNode:
    """One collapsed qi-layer boundary directory."""

    id: str
    label: str
    rel_path: str
    scan_root: str
    scan_root_kind: str
    has_agents: bool
    has_context: bool
    group: str
    agents_path: Path | None
    context_path: Path | None
    scan_root_path: Path
    boundary_dir: Path


def _empty_nodes_by_id() -> dict[str, BoundaryNode]:
    return {}


def _empty_boundary_key_to_id() -> dict[tuple[Path, Path], str]:
    return {}


def _empty_file_to_node_id() -> dict[Path, str]:
    return {}


def _empty_roots_by_name() -> dict[str, Path]:
    return {}


def _empty_root_entries() -> list[tuple[str, Path]]:
    return []


def _empty_inbound_from() -> dict[str, list[str]]:
    return {}


def _empty_collapsed_edges() -> list[tuple[str, str, str, int]]:
    return []


@dataclass
class GraphIndex:
    """Lookup tables shared by graph and content endpoints."""

    nodes_by_id: dict[str, BoundaryNode] = field(default_factory=_empty_nodes_by_id)
    boundary_key_to_id: dict[tuple[Path, Path], str] = field(
        default_factory=_empty_boundary_key_to_id
    )
    file_to_node_id: dict[Path, str] = field(default_factory=_empty_file_to_node_id)
    roots_by_name: dict[str, Path] = field(default_factory=_empty_roots_by_name)
    root_entries: list[tuple[str, Path]] = field(default_factory=_empty_root_entries)
    inbound_from: dict[str, list[str]] = field(default_factory=_empty_inbound_from)
    collapsed_edges: list[tuple[str, str, str, int]] = field(
        default_factory=_empty_collapsed_edges
    )


def _root_name_map(roots: list[ScanRoot]) -> dict[Path, str]:
    return {root.abs_path.resolve(): root.name for root in roots}


def _root_kind_map(roots: list[ScanRoot]) -> dict[str, str]:
    return {root.name: root.kind for root in roots}


def build_graph_index(
    analysis: AnalysisResult,
    discovery: DiscoveryResult,
) -> GraphIndex:
    """Collapse file nodes into boundary nodes and edge indexes."""

    name_for_root = _root_name_map(discovery.roots)
    kind_for_name = _root_kind_map(discovery.roots)
    index = GraphIndex(
        roots_by_name=discovery.name_to_path,
        root_entries=[(root.name, root.abs_path.resolve()) for root in discovery.roots],
    )

    groups: dict[tuple[Path, Path], dict[str, Path | None]] = defaultdict(
        lambda: {"agents": None, "context": None}
    )

    for file_path, node in analysis.nodes.items():
        scan_root = node.scan_root
        if scan_root is None:
            continue
        scan_root = scan_root.resolve()
        boundary = boundary_for_qi_doc(file_path).resolve()
        key = (scan_root, boundary)
        if file_path.name == _AGENTS_NAME:
            groups[key]["agents"] = file_path
        else:
            groups[key]["context"] = file_path

    for (scan_root, boundary), files in sorted(groups.items(), key=lambda item: item[0]):
        root_name = name_for_root.get(scan_root)
        if root_name is None:
            continue
        rel_path = _rel_boundary(boundary, scan_root)
        node_id = f"{root_name}:{rel_path}"
        agents_path = files["agents"]
        context_path = files["context"]
        boundary_node = BoundaryNode(
            id=node_id,
            label=_node_label(
                agents_path=agents_path,
                context_path=context_path,
                rel_path=rel_path,
                root_name=root_name,
            ),
            rel_path=rel_path,
            scan_root=root_name,
            scan_root_kind=kind_for_name[root_name],
            has_agents=agents_path is not None,
            has_context=context_path is not None,
            group=root_name,
            agents_path=agents_path,
            context_path=context_path,
            scan_root_path=scan_root,
            boundary_dir=boundary,
        )
        index.nodes_by_id[node_id] = boundary_node
        index.boundary_key_to_id[(scan_root, boundary)] = node_id
        for doc_path in (agents_path, context_path):
            if doc_path is not None:
                index.file_to_node_id[doc_path.resolve()] = node_id

    collapsed_edges = _collapse_edges(analysis, index)
    index.collapsed_edges = collapsed_edges
    inbound: dict[str, set[str]] = defaultdict(set)
    for source_id, target_id, _label, _weight in collapsed_edges:
        if source_id != target_id:
            inbound[target_id].add(source_id)

    index.inbound_from = {
        node_id: sorted(sources) for node_id, sources in sorted(inbound.items())
    }
    for node_id in index.nodes_by_id:
        index.inbound_from.setdefault(node_id, [])

    return index


def _collapse_edges(
    analysis: AnalysisResult,
    index: GraphIndex,
) -> list[tuple[str, str, str, int]]:
    """Return (source_id, target_id, label, weight) tuples."""

    pair_data: dict[tuple[str, str], dict[str, int | str]] = {}

    for edge in analysis.edges:
        if not edge.resolved or not isinstance(edge.dst, Path):
            continue
        source_id = index.file_to_node_id.get(edge.src.resolve())
        if source_id is None:
            continue
        resolution = resolve_local_target(edge.dst, index)
        if resolution.category != "cross-ref" or resolution.node_id is None:
            continue
        target_id = resolution.node_id
        if source_id == target_id:
            continue
        label = _edge_label(edge, analysis.nodes.get(edge.src))
        key = (source_id, target_id)
        existing = pair_data.get(key)
        if existing is None:
            pair_data[key] = {"label": label, "weight": 1}
        else:
            existing["weight"] = int(existing["weight"]) + 1

    return [
        (source_id, target_id, str(data["label"]), int(data["weight"]))
        for (source_id, target_id), data in sorted(pair_data.items())
    ]


def _edge_label(edge: GraphEdge, node: GraphNode | None) -> str:
    if node is None:
        return ""
    for ref in node.doc.references:
        if ref.line != edge.line:
            continue
        if isinstance(edge.dst, Path) and ref.resolved == edge.dst:
            return ref.text
        if ref.target == str(edge.dst):
            return ref.text
    return ""


def _outbound_link_count(node: BoundaryNode, analysis: AnalysisResult) -> int:
    count = 0
    for doc_path in (node.agents_path, node.context_path):
        if doc_path is None:
            continue
        file_node = analysis.nodes.get(doc_path)
        if file_node is None:
            continue
        for ref in file_node.doc.references:
            if ref.resolved is None and not is_external_link(ref.target):
                continue
            if ref.resolved is not None and not ref.resolved.exists():
                continue
            count += 1
    return count


def is_external_link(target: str) -> bool:
    return any(target.startswith(prefix) for prefix in _EXTERNAL_PREFIXES)


def analysis_to_graph(
    analysis: AnalysisResult,
    discovery: DiscoveryResult,
    *,
    index: GraphIndex | None = None,
) -> dict[str, Any]:
    """Convert analysis output into ``GET /api/graph`` JSON."""

    graph_index = index or build_graph_index(analysis, discovery)
    collapsed_edges = graph_index.collapsed_edges

    nodes: list[dict[str, Any]] = []
    for node_id in sorted(graph_index.nodes_by_id):
        node = graph_index.nodes_by_id[node_id]
        nodes.append(
            {
                "id": node.id,
                "label": node.label,
                "relPath": node.rel_path,
                "scanRoot": node.scan_root,
                "scanRootKind": node.scan_root_kind,
                "has": {"agents": node.has_agents, "context": node.has_context},
                "linkCount": _outbound_link_count(node, analysis),
                "group": node.group,
            }
        )

    links = [
        {
            "source": source_id,
            "target": target_id,
            "label": label,
            "weight": weight,
        }
        for source_id, target_id, label, weight in collapsed_edges
    ]

    scan_roots = [
        {
            "name": root.name,
            "absPath": root.abs_path.as_posix(),
            "kind": root.kind,
        }
        for root in discovery.roots
    ]

    return {
        "root": discovery.primary.as_posix(),
        "scanRoots": scan_roots,
        "nodes": nodes,
        "links": links,
        "nodeCount": len(nodes),
        "edgeCount": len(links),
    }


def build_graph_data(
    discovery: DiscoveryResult,
) -> tuple[dict[str, Any], GraphIndex, AnalysisResult]:
    """Run analysis and produce graph JSON plus shared indexes."""

    analysis = build_analysis(
        root=discovery.primary,
        roots=[root.abs_path for root in discovery.roots],
        file_filter=qi_file_filter,
        include_backlinks=False,
        include_clusters=False,
    )
    index = build_graph_index(analysis, discovery)
    graph = analysis_to_graph(analysis, discovery, index=index)
    return graph, index, analysis


__all__ = [
    "BoundaryNode",
    "GraphIndex",
    "LinkCategory",
    "ResolvedLocalTarget",
    "analysis_to_graph",
    "boundary_for_qi_doc",
    "build_graph_data",
    "build_graph_index",
    "is_external_link",
    "qi_file_filter",
    "resolve_local_target",
]
