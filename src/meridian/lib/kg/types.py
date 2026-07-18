"""Typed models for KG graph analysis.

These types are KG-specific. Extraction types live in lib/markdown/types.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from meridian.lib.markdown.types import ExtractedDocument

FindingSeverity = Literal["error", "warning"]
GraphEdgeKind = Literal["link", "image", "wikilink"]
FindingCategory = Literal[
    "broken_link", "read_error", "flag_block", "conflict_marker"
]


@dataclass
class GraphNode:
    """One document in the knowledge graph."""

    doc: ExtractedDocument
    rel_path: str  # path relative to root (display key), forward slashes
    in_degree: int = 0  # count of inbound links (computed)


@dataclass
class GraphEdge:
    """Directed edge in the document graph."""

    src: Path  # source document
    dst: Path | str  # resolved Path for local links; raw str for wikilinks/unresolved
    kind: GraphEdgeKind
    resolved: bool  # True if dst exists on filesystem
    line: int  # source line number


def _empty_nodes() -> dict[Path, GraphNode]:
    return {}


def _empty_edges() -> list[GraphEdge]:
    return []


def _empty_paths() -> list[Path]:
    return []


def _empty_path_pairs() -> list[tuple[Path, Path]]:
    return []


def _empty_clusters() -> list[list[Path]]:
    return []


@dataclass
class AnalysisResult:
    """Complete KG analysis output."""

    nodes: dict[Path, GraphNode] = field(default_factory=_empty_nodes)
    edges: list[GraphEdge] = field(default_factory=_empty_edges)
    broken_links: list[GraphEdge] = field(default_factory=_empty_edges)
    orphans: list[Path] = field(default_factory=_empty_paths)
    missing_backlinks: list[tuple[Path, Path]] = field(default_factory=_empty_path_pairs)
    clusters: list[list[Path]] = field(default_factory=_empty_clusters)
    external_count: int = 0


@dataclass
class CheckFinding:
    """One finding from kg check."""

    category: FindingCategory
    severity: FindingSeverity
    file: Path
    line: int
    message: str
    detail: str | None = None  # e.g. the link target, the flag text


def _empty_findings() -> list[CheckFinding]:
    return []


@dataclass
class CheckResult:
    """Complete kg check output."""

    analysis: AnalysisResult
    findings: list[CheckFinding] = field(default_factory=_empty_findings)
    strict: bool = False

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == "warning" for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")


__all__ = [
    "AnalysisResult",
    "CheckFinding",
    "CheckResult",
    "FindingSeverity",
    "GraphEdge",
    "GraphNode",
]
