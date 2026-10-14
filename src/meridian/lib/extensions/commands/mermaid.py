"""Mermaid diagram validation first-party extension command."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from meridian.lib.extensions.context import (
    ExtensionCommandServices,
    ExtensionInvocationContext,
)
from meridian.lib.extensions.types import (
    ExtensionCommandSpec,
    ExtensionErrorResult,
    ExtensionJSONResult,
    ExtensionResult,
    ExtensionSurface,
)


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


async def mermaid_check_handler(
    args: dict[str, Any],
    context: ExtensionInvocationContext,
    services: ExtensionCommandServices,
) -> ExtensionResult:
    """Validate mermaid diagram syntax in a file or directory.

    Returns ExtensionErrorResult with code 'node_not_found' when Node.js
    is absent, 'bundle_not_found' when the bundled validator is missing,
    'not_found' when the target path does not exist, and 'args_invalid'
    when the provided path cannot be resolved. Returns ExtensionJSONResult
    on success (including when no mermaid blocks are found — that is not
    an error).
    """

    _ = (context, services)  # no invocation state needed

    from meridian.lib.mermaid.validator import (
        BundleNotFoundError,
        NodeNotFoundError,
        validate_path,
    )

    try:
        target = Path(args["path"]).resolve()
    except Exception as exc:
        return ExtensionErrorResult(
            code="args_invalid",
            message=f"invalid path: {exc}",
        )

    if not target.exists():
        return ExtensionErrorResult(
            code="not_found",
            message=f"path not found: {args['path']}",
        )

    try:
        result = validate_path(target)
    except NodeNotFoundError as exc:
        return ExtensionErrorResult(
            code="node_not_found",
            message=str(exc),
        )
    except BundleNotFoundError as exc:
        return ExtensionErrorResult(
            code="bundle_not_found",
            message=str(exc),
        )

    return ExtensionJSONResult(
        payload={
            "path": result.path,
            "total_blocks": result.total_blocks,
            "valid_blocks": result.valid_blocks,
            "invalid_blocks": result.invalid_blocks,
            "has_errors": result.has_errors,
            "results": [
                {
                    "file": r.file,
                    "line": r.line,
                    "valid": r.valid,
                    "error": r.error,
                }
                for r in result.results
            ],
        }
    )


MERMAID_CHECK_SPEC = ExtensionCommandSpec(
    extension_id="meridian.mermaid",
    command_id="check",
    summary="Validate mermaid diagram syntax in a markdown file or directory",
    args_schema=MermaidCheckArgs,
    result_schema=MermaidCheckResult,
    handler=mermaid_check_handler,
    surfaces=frozenset(
        {
            ExtensionSurface.CLI,
            ExtensionSurface.MCP,
        }
    ),
    first_party=True,
    # No cli_group/cli_name — extension-only, no direct CLI routing (D26).
    # No sync_handler — direct CLI bypasses extension dispatch (D27).
)
