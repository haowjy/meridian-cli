"""The ``meridian.mermaid.check`` operation for MCP and ``ext run``: syntax only."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from meridian.lib.extensions.types import ExtensionErrorResult


class MermaidCheckArgs(BaseModel):
    """Arguments for meridian.mermaid.check."""

    path: str = Field(description="File or directory containing mermaid diagrams to validate")


class BlockResultSchema(BaseModel):
    """Per-block validation result."""

    file: str
    line: int
    valid: bool
    error: str | None = None


class MermaidCheckResult(BaseModel):
    """Result payload for meridian.mermaid.check."""

    path: str
    total_blocks: int
    valid_blocks: int
    invalid_blocks: int
    has_errors: bool
    results: list[BlockResultSchema]


def mermaid_check_sync(args: MermaidCheckArgs) -> MermaidCheckResult | ExtensionErrorResult:
    """Validate mermaid diagram syntax in a file or directory.

    A path with no diagrams is a success with zero blocks, not an error.
    """

    from meridian.lib.mermaid.check import check_path
    from meridian.lib.mermaid.style.types import StyleCheckOptions

    try:
        target = Path(args.path).resolve()
    except (OSError, RuntimeError, ValueError) as exc:  # NUL byte; symlink loop before 3.13
        return ExtensionErrorResult(code="args_invalid", message=f"invalid path: {exc}")
    if not target.exists():
        return ExtensionErrorResult(code="not_found", message=f"path not found: {args.path}")
    result = check_path(target, style=StyleCheckOptions(enabled=False))
    return MermaidCheckResult.model_validate(result.validation, from_attributes=True)
