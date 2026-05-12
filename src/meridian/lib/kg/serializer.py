"""JSON serialization for KG analysis results.

Provides the stable JSON contract for API routes and machine consumers.
All paths are expressed as strings relative to root, using forward slashes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meridian.lib.kg.types import AnalysisResult, CheckResult


def serialize_analysis(result: AnalysisResult, root: Path) -> dict[str, Any]:
    """Serialize AnalysisResult to JSON-safe dict.

    All paths are expressed as strings relative to root, using forward slashes.
    """
    return {
        "root": root.as_posix(),
        "total_files": len(result.nodes),
        "total_links": len(result.edges),
        "external_count": result.external_count,
        "has_broken_links": len(result.broken_links) > 0,
        "broken_links": [
            {
                "source": _rel(edge.src, root),
                "target": str(edge.dst) if isinstance(edge.dst, str) else _rel(edge.dst, root),
                "line": edge.line,
                "kind": edge.kind,
            }
            for edge in result.broken_links
        ],
        "orphans": [_rel(p, root) for p in result.orphans],
        "missing_backlinks": [[_rel(a, root), _rel(b, root)] for a, b in result.missing_backlinks],
        "clusters": [[_rel(p, root) for p in cluster] for cluster in result.clusters],
        "summary": {
            "total_files": len(result.nodes),
            "total_links": len(result.edges),
            "external_links": result.external_count,
            "broken_links": len(result.broken_links),
            "orphans": len(result.orphans),
            "missing_backlinks": len(result.missing_backlinks),
            "clusters": len(result.clusters),
        },
    }


def serialize_check(result: AnalysisResult, path: Path) -> dict[str, Any]:
    """Serialize a targeted check result for kg check command.

    Focused output for single file or directory analysis.
    """
    root = path if path.is_dir() else path.parent

    return {
        "path": path.as_posix(),
        "total_files": len(result.nodes),
        "external_count": result.external_count,
        "has_broken_links": len(result.broken_links) > 0,
        "broken_links": [
            {
                "source": _rel(edge.src, root),
                "target": str(edge.dst) if isinstance(edge.dst, str) else _rel(edge.dst, root),
                "line": edge.line,
                "kind": edge.kind,
            }
            for edge in result.broken_links
        ],
    }


def serialize_check_findings(result: CheckResult, path: Path) -> dict[str, Any]:
    """Serialize a multi-category kg check result."""
    root = path if path.is_dir() else path.parent
    category_counts = {
        "broken_links": sum(1 for f in result.findings if f.category == "broken_link"),
        "flag_blocks": sum(1 for f in result.findings if f.category == "flag_block"),
        "conflict_markers": sum(1 for f in result.findings if f.category == "conflict_marker"),
    }

    return {
        "path": path.as_posix(),
        "total_files": len(result.analysis.nodes),
        "findings": [
            {
                "category": finding.category,
                "severity": finding.severity,
                "file": _rel(finding.file, root),
                "line": finding.line,
                "message": finding.message,
                "detail": finding.detail,
            }
            for finding in result.findings
        ],
        "counts": {
            "errors": result.error_count,
            "warnings": result.warning_count,
            **category_counts,
        },
        "strict": result.strict,
        "has_errors": result.has_errors,
    }


def _rel(path: Path, root: Path) -> str:
    """Get path relative to root as forward-slash string."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "serialize_analysis",
    "serialize_check",
    "serialize_check_findings",
]
