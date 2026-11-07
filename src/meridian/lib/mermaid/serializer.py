from __future__ import annotations

from pathlib import Path
from typing import Any

from meridian.lib.mermaid.style.types import CheckResult


def serialize_check(result: CheckResult, path: Path) -> dict[str, Any]:
    validation = result.validation
    return {
        "path": path.as_posix(),
        "tier": validation.tier,
        "total_blocks": validation.total_blocks,
        "valid_blocks": validation.valid_blocks,
        "invalid_blocks": validation.invalid_blocks,
        "has_errors": validation.has_errors,
        "total_warnings": len(result.warnings),
        "suppressed_warnings": len(result.suppressed_warnings),
        "warnings": [
            {
                "category": warning.category,
                "file": warning.file,
                "line": warning.line,
                "message": warning.message,
                "severity": warning.severity,
            }
            for warning in result.warnings
        ],
        "suppressed": [
            {
                "category": warning.category,
                "file": warning.file,
                "line": warning.line,
                "message": warning.message,
                "severity": warning.severity,
                "suppression_source": warning.suppression_source,
            }
            for warning in result.suppressed_warnings
        ],
        "results": [
            {
                "file": block.file,
                "line": block.line,
                "valid": block.valid,
                "error": block.error,
                "diagram_type": block.diagram_type,
            }
            for block in validation.results
        ],
    }


__all__ = ["serialize_check"]
