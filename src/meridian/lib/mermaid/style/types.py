from __future__ import annotations

from dataclasses import dataclass, field

from meridian.lib.mermaid.validator import MermaidValidationResult


@dataclass
class StyleWarning:
    """A Mermaid style warning emitted for a diagram block."""

    category: str
    file: str
    line: int
    message: str
    severity: str
    suppressed: bool
    suppression_source: str | None


@dataclass
class WarningCategory:
    """Metadata describing a Mermaid style warning category."""

    id: str
    description: str
    default: bool
    diagram_types: frozenset[str] | None


@dataclass
class StyleCheckOptions:
    """Options controlling Mermaid style checks."""

    enabled: bool = True
    strict: bool = False
    disabled_categories: set[str] = field(default_factory=lambda: set[str]())


@dataclass
class CheckResult:
    """Aggregate result: validation + style warnings."""

    validation: MermaidValidationResult
    warnings: list[StyleWarning] = field(default_factory=lambda: list[StyleWarning]())
    suppressed_warnings: list[StyleWarning] = field(default_factory=lambda: list[StyleWarning]())
    style_options: StyleCheckOptions = field(default_factory=StyleCheckOptions)


__all__ = ["CheckResult", "StyleCheckOptions", "StyleWarning", "WarningCategory"]
