from __future__ import annotations

from collections.abc import Callable

from meridian.lib.mermaid.scanner import DiagramTarget
from meridian.lib.mermaid.style.bare_end import BARE_END_CATEGORY, check_bare_end
from meridian.lib.mermaid.style.fill_no_color import FILL_NO_COLOR_CATEGORY, check_fill_no_color
from meridian.lib.mermaid.style.ox_edge import OX_EDGE_CATEGORY, check_ox_edge
from meridian.lib.mermaid.style.suppression import parse_suppressions
from meridian.lib.mermaid.style.types import StyleCheckOptions, StyleWarning, WarningCategory
from meridian.lib.mermaid.validator import BlockResult

CheckFn = Callable[[DiagramTarget, str | None], list[StyleWarning]]

_PRE_PARSE: list[tuple[WarningCategory, CheckFn]] = [
    (BARE_END_CATEGORY, check_bare_end),
    (OX_EDGE_CATEGORY, check_ox_edge),
]
_POST_PARSE: list[tuple[WarningCategory, CheckFn]] = [
    (FILL_NO_COLOR_CATEGORY, check_fill_no_color),
]


def run_style_checks(
    targets: list[DiagramTarget],
    validation_results: list[BlockResult],
    options: StyleCheckOptions,
) -> tuple[list[StyleWarning], list[StyleWarning]]:
    """Run style checks on all targets.

    Returns (active_warnings, suppressed_warnings).

    Pre-parse checks run on ALL targets (valid and invalid).
    Post-parse checks run only on targets that passed syntax validation.
    Pre-parse warnings are deduplicated against syntax errors for the same block.
    Skips categories in options.disabled_categories.
    """
    if not options.enabled:
        return [], []

    result_by_location = {(result.file, result.line): result for result in validation_results}

    raw_warnings: list[tuple[DiagramTarget, StyleWarning]] = []

    for target in targets:
        validation_result = result_by_location.get((target.rel, target.start_line))
        diagram_type = validation_result.diagram_type if validation_result else None
        block_has_error = validation_result is not None and not validation_result.valid

        for category, check in _PRE_PARSE:
            if _should_skip_category(category, diagram_type, options):
                continue
            warnings = check(target, diagram_type)
            if block_has_error:
                continue
            raw_warnings.extend((target, warning) for warning in warnings)

        if validation_result is None or block_has_error:
            continue

        for category, check in _POST_PARSE:
            if _should_skip_category(category, diagram_type, options):
                continue
            raw_warnings.extend((target, warning) for warning in check(target, diagram_type))

    active_warnings: list[StyleWarning] = []
    suppressed_warnings: list[StyleWarning] = []
    suppression_by_target = {id(target): parse_suppressions(target.content) for target in targets}

    for target, warning in raw_warnings:
        content_line = _file_line_to_content_line(target, warning.line)
        suppressions = suppression_by_target[id(target)]
        suppressed, source = suppressions.is_suppressed(content_line, warning.category)
        if suppressed:
            warning.suppressed = True
            warning.suppression_source = source
            suppressed_warnings.append(warning)
        else:
            active_warnings.append(warning)

    return active_warnings, suppressed_warnings


def get_all_categories() -> list[WarningCategory]:
    """Return all registered Mermaid style warning categories."""
    return [category for category, _check in [*_PRE_PARSE, *_POST_PARSE]]


def _should_skip_category(
    category: WarningCategory,
    diagram_type: str | None,
    options: StyleCheckOptions,
) -> bool:
    """Return True when a category should not run for this target."""
    if category.id in options.disabled_categories:
        return True
    return category.diagram_types is not None and diagram_type not in category.diagram_types


def _file_line_to_content_line(target: DiagramTarget, file_line: int) -> int:
    if target.source == "fenced-block":
        return file_line - target.start_line
    return file_line


__all__ = [
    "get_all_categories",
    "run_style_checks",
]
