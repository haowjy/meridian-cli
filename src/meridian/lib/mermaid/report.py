from __future__ import annotations

from meridian.lib.mermaid.style.types import CheckResult


def format_check_output(result: CheckResult) -> tuple[str, str]:
    validation = result.validation
    stdout_lines: list[str] = []

    for block in validation.results:
        if not block.valid:
            tier_tag = f" [{block.tier}]" if block.tier == "python" else ""
            error_msg = block.error or "unknown error"
            stdout_lines.append(f"{block.file}:{block.line}: {error_msg}{tier_tag}")

    for warning in result.warnings:
        stdout_lines.append(
            f"warning[{warning.category}] {warning.file}:{warning.line}: {warning.message}"
        )

    summary = _format_summary(result)
    if summary:
        if stdout_lines:
            stdout_lines.append("")
        stdout_lines.append(summary)

    stdout = "\n".join(stdout_lines)
    stderr = f"{validation.invalid_blocks} invalid block(s) found" if validation.has_errors else ""
    return stdout, stderr


def _format_summary(result: CheckResult) -> str:
    validation = result.validation
    warning_count = len(result.warnings)

    if validation.has_errors and warning_count == 0:
        return ""

    block_summary = _format_valid_block_count(validation.valid_blocks, validation.total_blocks)
    if warning_count == 0:
        return block_summary
    return f"{block_summary}; {warning_count} style warning{'' if warning_count == 1 else 's'}"


def _format_valid_block_count(valid_blocks: int, total_blocks: int) -> str:
    if total_blocks == 0:
        return "✓ No mermaid blocks found"
    if valid_blocks == 1:
        return "✓ 1 mermaid block valid"
    if valid_blocks == total_blocks:
        return f"✓ All {total_blocks} mermaid blocks valid"
    return f"✓ {valid_blocks} mermaid blocks valid"


__all__ = ["format_check_output"]
