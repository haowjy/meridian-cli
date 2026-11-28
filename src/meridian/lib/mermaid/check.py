"""One Mermaid check for every surface: collect the diagrams once, validate, then style."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.mermaid.scanner import collect_targets
from meridian.lib.mermaid.style import run_style_checks
from meridian.lib.mermaid.style.types import CheckResult, StyleCheckOptions
from meridian.lib.mermaid.validator import validate_targets


def check_path(
    path: Path,
    *,
    depth: int | None = None,
    exclude: list[str] | None = None,
    style: StyleCheckOptions | None = None,
) -> CheckResult:
    """Check every Mermaid diagram in the existing file or directory *path*."""

    resolved = path.resolve()
    root = resolved if resolved.is_dir() else resolved.parent
    targets = collect_targets(resolved, root, depth=depth, exclude=exclude or None)
    validation = validate_targets(resolved, targets)
    style_options = style or StyleCheckOptions()
    warnings, suppressed = run_style_checks(targets, validation.results, style_options)
    return CheckResult(
        validation=validation,
        warnings=warnings,
        suppressed_warnings=suppressed,
        style_options=style_options,
    )


__all__ = ["check_path"]
