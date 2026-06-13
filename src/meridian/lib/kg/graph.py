"""KG graph analysis — build AnalysisResult from markdown files."""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pathspec
from markdown_it import MarkdownIt

from meridian.lib.ignores import SKIP_DIRS, load_ignore_patterns
from meridian.lib.kg.types import (
    AnalysisResult,
    CheckFinding,
    CheckResult,
    FindingSeverity,
    GraphEdge,
    GraphNode,
)
from meridian.lib.markdown.extract import extract_file
from meridian.lib.markdown.types import ExtractedLink

# External URL prefixes — not checked for broken links
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")
_CONFLICT_START = "<<<<<<<"
_CONFLICT_SEPARATOR = "======="
_CONFLICT_END = ">>>>>>>"
_ACTIONABLE_FLAG_RE = re.compile(r"^\s*>\s*\[!FLAG\]")
_md_parser = MarkdownIt()


def build_analysis(
    root: Path,
    *,
    include_backlinks: bool = True,
    include_clusters: bool = True,
    targeted_path: Path | None = None,
    exclude: list[str] | None = None,
    roots: list[Path] | None = None,
    file_filter: Callable[[Path], bool] | None = None,
) -> AnalysisResult:
    """Build complete KG analysis from a root directory.

    Args:
        root: Root directory to scan for .md files
        include_backlinks: Whether to compute missing backlinks
        include_clusters: Whether to compute connected clusters
        targeted_path: If set, only analyze this file/directory (for kg check).
            Ignored when ``roots`` is set.
        exclude: Glob patterns to exclude from the scan
        roots: When set, scan every root and merge nodes. ``targeted_path`` is
            ignored.
        file_filter: When set, only include .md files for which this returns True.

    Returns:
        AnalysisResult with nodes, edges, broken_links, orphans, etc.
    """
    root = root.resolve()

    if roots is not None:
        resolved_roots = [r.resolve() for r in roots]
        file_to_scan_root = _collect_multi_root_files(
            resolved_roots,
            exclude=exclude,
            file_filter=file_filter,
        )
    elif targeted_path is not None:
        targeted = targeted_path.resolve()
        if targeted.is_file():
            md_files = (
                [targeted]
                if file_filter is None or file_filter(targeted)
                else []
            )
            scan_root = targeted.parent
        else:
            md_files = _collect_md_files(
                targeted, exclude=exclude, file_filter=file_filter
            )
            scan_root = targeted
        file_to_scan_root = {p: scan_root for p in md_files}
    else:
        md_files = _collect_md_files(root, exclude=exclude, file_filter=file_filter)
        file_to_scan_root = {p: root for p in md_files}

    # Build nodes dict
    nodes: dict[Path, GraphNode] = {}
    for md_path, scan_root in file_to_scan_root.items():
        doc = extract_file(md_path)
        rel_path = _rel_posix(md_path, scan_root)
        nodes[md_path] = GraphNode(
            doc=doc, rel_path=rel_path, in_degree=0, scan_root=scan_root
        )

    # Build edges and track inbound links
    edges: list[GraphEdge] = []
    broken_links: list[GraphEdge] = []
    inbound: dict[Path, set[Path]] = {p: set() for p in nodes}
    external_count = 0

    for src_path, node in nodes.items():
        for ref in node.doc.references:
            edge = _make_edge(src_path, ref, nodes)
            edges.append(edge)

            if _is_external(ref.target):
                external_count += 1
            elif not edge.resolved:
                broken_links.append(edge)
            elif isinstance(edge.dst, Path) and edge.dst in nodes:
                inbound[edge.dst].add(src_path)

    # Compute in_degree for each node
    for target_path, sources in inbound.items():
        nodes[target_path].in_degree = len(sources)

    # Orphan detection removed from output; retain field for compatibility.
    orphans: list[Path] = []

    # Missing backlinks (A→B but no B→A)
    missing_backlinks: list[tuple[Path, Path]] = []
    if include_backlinks:
        for src_path, node in nodes.items():
            for ref in node.doc.references:
                if isinstance(ref.resolved, Path) and ref.resolved in nodes:
                    target = ref.resolved
                    # Check if target links back to src
                    target_links_back = any(
                        r.resolved == src_path for r in nodes[target].doc.references
                    )
                    if not target_links_back:
                        pair = (src_path, target)
                        if pair not in missing_backlinks:
                            missing_backlinks.append(pair)

    # Compute connected clusters via BFS
    clusters: list[list[Path]] = []
    if include_clusters:
        clusters = _compute_clusters(nodes, inbound)

    return AnalysisResult(
        nodes=nodes,
        edges=edges,
        broken_links=broken_links,
        orphans=orphans,
        missing_backlinks=missing_backlinks,
        clusters=clusters,
        external_count=external_count,
    )


def build_check(
    root: Path,
    *,
    targeted_path: Path | None = None,
    exclude: list[str] | None = None,
    strict: bool = False,
) -> CheckResult:
    """Build kg check result with broken links and content findings."""
    analysis = build_analysis(
        root=root,
        include_backlinks=False,
        include_clusters=False,
        targeted_path=targeted_path,
        exclude=exclude,
    )

    warning_severity: FindingSeverity = "error" if strict else "warning"
    findings: list[CheckFinding] = []

    for edge in analysis.broken_links:
        target = str(edge.dst)
        findings.append(
            CheckFinding(
                category="broken_link",
                severity=warning_severity,
                file=edge.src,
                line=edge.line,
                message=f"Broken link: {target}",
                detail=target,
            )
        )

    for path in sorted(analysis.nodes):
        findings.extend(_scan_file_findings(path, warning_severity))

    return CheckResult(analysis=analysis, findings=findings, strict=strict)


def _scan_file_findings(path: Path, warning_severity: FindingSeverity) -> list[CheckFinding]:
    findings: list[CheckFinding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        findings.append(
            CheckFinding(
                category="read_error",
                severity="error",
                file=path,
                line=0,
                message=f"Could not read file: {exc}",
                detail=str(exc),
            )
        )
        return findings

    lines = text.splitlines()
    fenced = _fenced_lines(text)
    in_conflict = False
    for line_number, line in enumerate(lines, start=1):
        if line_number in fenced:
            continue

        if _ACTIONABLE_FLAG_RE.match(line):
            findings.append(
                CheckFinding(
                    category="flag_block",
                    severity=warning_severity,
                    file=path,
                    line=line_number,
                    message="Flag block found",
                    detail=line.strip(),
                )
            )

        if line.startswith(_CONFLICT_START):
            in_conflict = True
            findings.append(_conflict_marker_finding(path, line_number, line))
        elif in_conflict and line.startswith(_CONFLICT_SEPARATOR):
            findings.append(_conflict_marker_finding(path, line_number, line))
        elif line.startswith(_CONFLICT_END):
            findings.append(_conflict_marker_finding(path, line_number, line))
            in_conflict = False

    return findings


def _fenced_lines(text: str) -> set[int]:
    """Return 1-indexed line numbers covered by fenced code block tokens."""
    fenced: set[int] = set()
    for token in _md_parser.parse(text):
        if token.type == "fence" and token.map:
            start_line, end_line = token.map
            for line_0 in range(start_line, end_line):
                fenced.add(line_0 + 1)
    return fenced


def _conflict_marker_finding(path: Path, line_number: int, line: str) -> CheckFinding:
    return CheckFinding(
        category="conflict_marker",
        severity="error",
        file=path,
        line=line_number,
        message="Git conflict marker found",
        detail=line.strip(),
    )


def _is_external(target: str) -> bool:
    """Check if a target is an external URL."""
    return any(target.startswith(prefix) for prefix in _EXTERNAL_PREFIXES)


def _collect_multi_root_files(
    roots: list[Path],
    *,
    exclude: list[str] | None = None,
    file_filter: Callable[[Path], bool] | None = None,
) -> dict[Path, Path]:
    """Collect .md files from multiple roots; attribute nested files to longest root."""
    file_to_scan_root: dict[Path, Path] = {}
    for scan_root in roots:
        for md_path in _collect_md_files(
            scan_root, exclude=exclude, file_filter=file_filter
        ):
            existing = file_to_scan_root.get(md_path)
            if existing is None or len(scan_root.parts) > len(existing.parts):
                file_to_scan_root[md_path] = scan_root
    return file_to_scan_root


def _collect_md_files(
    root: Path,
    *,
    exclude: list[str] | None = None,
    file_filter: Callable[[Path], bool] | None = None,
) -> list[Path]:
    """Walk directory and collect .md files, applying skip dirs and exclusions."""
    kgignore_spec = load_ignore_patterns(root, ".kgignore")

    exclude_spec: pathspec.PathSpec[Any] | None = None
    if exclude:
        exclude_spec = cast(
            "pathspec.PathSpec[Any]",
            pathspec.PathSpec.from_lines("gitwildmatch", exclude),  # pyright: ignore[reportUnknownMemberType]
        )

    md_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped directories in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for filename in filenames:
            if not filename.endswith(".md"):
                continue

            full_path = Path(dirpath) / filename
            if file_filter is not None and not file_filter(full_path):
                continue
            rel = full_path.relative_to(root).as_posix()

            if kgignore_spec and kgignore_spec.match_file(rel):
                continue
            if exclude_spec and exclude_spec.match_file(rel):
                continue

            md_files.append(full_path)

    return sorted(md_files)


def _make_edge(
    src_path: Path,
    ref: ExtractedLink,
    nodes: dict[Path, GraphNode],
) -> GraphEdge:
    """Convert an ExtractedLink to a GraphEdge."""
    _ = nodes  # Available for future use (e.g., node-aware resolution)

    resolved: bool
    dst: Path | str

    if ref.resolved is not None:
        dst = ref.resolved
        resolved = ref.resolved.exists()
    else:
        # Wikilinks or external — store as raw string
        dst = ref.target
        # External links are "resolved" (we just don't check them)
        resolved = _is_external(ref.target)

    return GraphEdge(
        src=src_path,
        dst=dst,
        kind=ref.kind,
        resolved=resolved,
        line=ref.line,
    )


def _compute_clusters(
    nodes: dict[Path, GraphNode], inbound: dict[Path, set[Path]]
) -> list[list[Path]]:
    """Compute connected components via BFS over bidirectional adjacency."""
    # Build bidirectional adjacency
    adj: dict[Path, set[Path]] = {p: set() for p in nodes}
    for target, sources in inbound.items():
        for src in sources:
            adj[src].add(target)
            adj[target].add(src)

    visited: set[Path] = set()
    clusters: list[list[Path]] = []

    for start in nodes:
        if start in visited:
            continue
        # BFS from start
        component: list[Path] = []
        queue: deque[Path] = deque([start])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            for neighbor in adj[current]:
                if neighbor not in visited:
                    queue.append(neighbor)
        # Only include non-singleton clusters (singletons are orphans)
        if len(component) > 1:
            clusters.append(sorted(component))

    return clusters


def _rel_posix(path: Path, root: Path) -> str:
    """Get path relative to root as forward-slash string."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = ["build_analysis", "build_check"]
