"""Plain-text report formatting for KG analysis results."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.kg.types import AnalysisResult, CheckFinding, CheckResult

_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")
_ChildKind = Literal["local", "external", "leaf", "broken"]


def _is_external_str(target: str) -> bool:
    return any(target.startswith(prefix) for prefix in _EXTERNAL_PREFIXES)


def _rel(path: Path, root: Path) -> str:
    """Get path relative to root as forward-slash string."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


@dataclass
class _Child:
    """One outbound link from a node."""

    dst: Path | str  # Path for local, str for external or broken
    kind: _ChildKind
    line: int


def format_summary(result: AnalysisResult) -> str:
    """Return compact single-line summary string."""
    parts = [
        f"{len(result.nodes)} files",
        f"{len(result.edges)} links",
        f"{result.external_count} external",
        f"{len(result.broken_links)} broken",
    ]
    return "  ".join(parts)


def format_root_summary(result: AnalysisResult, root: Path) -> str:
    """Format output for bare 'meridian kg' command."""
    lines = [
        f"Knowledge Graph: {root.as_posix()}",
        f"  {format_summary(result)}",
        "",
        "  meridian kg graph           Show link topology tree",
        "  meridian kg graph --depth 5 Show tree 5 hops deep (default: 3)",
        "  meridian kg check           Verify broken links, exit 1 if any found",
    ]
    return "\n".join(lines)


def format_tree(
    result: AnalysisResult,
    root: Path,
    *,
    depth: int = 3,
    show_external: bool = False,
) -> str:
    """Render link topology tree for kg graph command.

    Args:
        result: Analysis result from build_analysis().
        root: Scan root directory for path relativization.
        depth: Max link-hops to traverse from each tree root. Default 3.
        show_external: If True, show external URLs as leaf nodes.
    """
    outbound: dict[Path, list[_Child]] = defaultdict(list)
    inbound_count: dict[Path, int] = defaultdict(int)

    for edge in result.edges:
        dst = edge.dst
        if isinstance(dst, str) and _is_external_str(dst):
            if show_external:
                outbound[edge.src].append(_Child(dst=dst, kind="external", line=edge.line))
        elif isinstance(dst, Path) and dst in result.nodes:
            outbound[edge.src].append(_Child(dst=dst, kind="local", line=edge.line))
            inbound_count[dst] += 1
        elif isinstance(dst, Path) and edge.resolved:
            # Link target exists on disk but not in our scanned nodes
            # (e.g., single-file mode). Show as leaf, not expandable.
            outbound[edge.src].append(_Child(dst=dst, kind="leaf", line=edge.line))
        else:
            # Truly broken link
            dst_str = str(dst)
            outbound[edge.src].append(_Child(dst=dst_str, kind="broken", line=edge.line))

    all_nodes = set(result.nodes.keys())

    def local_out_count(node: Path) -> int:
        return sum(1 for c in outbound[node] if c.kind == "local")

    # Single-file mode: that file is the tree root
    if len(all_nodes) == 1:
        tree_roots = list(all_nodes)
    else:
        tree_roots = sorted(
            node for node in all_nodes if inbound_count[node] == 0 and local_out_count(node) > 0
        )

    lines: list[str] = [f"root: {root.as_posix()}", ""]
    shown: set[Path] = set()

    for root_node in tree_roots:
        lines.append(_rel(root_node, root))
        shown.add(root_node)
        if depth <= 0:
            continue  # depth 0 = show roots only, no children
        children = outbound[root_node]
        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            _render_child(child, is_last, "", depth - 1, shown, outbound, root, lines)

    return "\n".join(lines)


def _render_child(
    child: _Child,
    is_last: bool,
    prefix: str,
    depth_remaining: int,
    shown: set[Path],
    outbound: dict[Path, list[_Child]],
    root: Path,
    lines: list[str],
) -> None:
    """Recursively render one child node into lines."""
    connector = "└──" if is_last else "├──"
    child_prefix = prefix + ("    " if is_last else "│   ")

    if child.kind == "external":
        lines.append(f"{prefix}{connector} {child.dst}")
        return

    if child.kind == "broken":
        lines.append(f"{prefix}{connector} {child.dst} (not found)")
        return

    if child.kind == "leaf":
        # Exists but not in scanned nodes - show as leaf
        assert isinstance(child.dst, Path)
        lines.append(f"{prefix}{connector} {_rel(child.dst, root)}")
        return

    assert isinstance(child.dst, Path)
    node = child.dst
    label = _rel(node, root)

    if node in shown:
        lines.append(f"{prefix}{connector} {label} (already shown)")
        return

    if depth_remaining <= 0:
        local_out = sum(1 for c in outbound[node] if c.kind == "local")
        annotation = f" ({local_out} links hidden)" if local_out else ""
        lines.append(f"{prefix}{connector} {label}{annotation}")
        return

    lines.append(f"{prefix}{connector} {label}")
    shown.add(node)
    grandchildren = outbound[node]
    for i, grandchild in enumerate(grandchildren):
        gc_is_last = i == len(grandchildren) - 1
        _render_child(
            grandchild,
            gc_is_last,
            child_prefix,
            depth_remaining - 1,
            shown,
            outbound,
            root,
            lines,
        )


def format_check_output(result: AnalysisResult, root: Path) -> tuple[str, str]:
    """Format output for kg check. Returns (stdout, stderr)."""
    total_files = len(result.nodes)
    total_links = len(result.edges)

    if not result.broken_links:
        return f"No broken links ({total_files} files, {total_links} links)", ""

    stdout_lines: list[str] = []
    for edge in result.broken_links:
        src_rel = _rel(edge.src, root)
        target_str = str(edge.dst) if isinstance(edge.dst, str) else _rel(edge.dst, root)
        stdout_lines.append(f"  {src_rel}:{edge.line} -> {target_str} [{edge.kind}]")

    stdout = "\n".join(stdout_lines)
    stderr = f"{len(result.broken_links)} broken links ({total_files} files, {total_links} links)"
    return stdout, stderr


def format_check_findings(result: CheckResult, root: Path) -> tuple[str, str]:
    """Format check findings for text output. Returns (stdout, stderr)."""
    total_files = len(result.analysis.nodes)
    total_links = len(result.analysis.edges)

    if not result.findings:
        return f"No issues found ({total_files} files, {total_links} links)", ""

    findings_by_file: dict[Path, list[CheckFinding]] = defaultdict(list)
    for finding in result.findings:
        findings_by_file[finding.file].append(finding)

    stdout_lines: list[str] = []
    for file in sorted(findings_by_file):
        for finding in sorted(
            findings_by_file[file],
            key=lambda item: (item.line, item.category),
        ):
            rel_path = _rel(finding.file, root)
            stdout_lines.append(
                f"  {finding.severity}: {rel_path}:{finding.line} {finding.message}"
            )

    stderr = (
        f"{result.error_count} errors, {result.warning_count} warnings "
        f"({total_files} files scanned)"
    )
    return "\n".join(stdout_lines), stderr


__all__ = [
    "format_check_findings",
    "format_check_output",
    "format_root_summary",
    "format_summary",
    "format_tree",
]
