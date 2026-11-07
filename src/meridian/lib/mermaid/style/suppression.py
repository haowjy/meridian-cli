from __future__ import annotations

from dataclasses import dataclass, field

_IGNORE = "mermaid-check-ignore"
_IGNORE_NEXT = "mermaid-check-ignore-next-line"


@dataclass
class SuppressionSet:
    """Tracks suppression directives parsed from a Mermaid block."""

    block_suppressions: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    block_suppress_all: int | None = None
    next_line_suppressions: dict[tuple[int, str | None], int] = field(
        default_factory=lambda: dict[tuple[int, str | None], int]()
    )

    def is_suppressed(self, content_line: int, category: str) -> tuple[bool, str | None]:
        """Check whether a category warning on a content line is suppressed."""
        block_suppressed, block_source = self.is_block_suppressed(category)
        if block_suppressed:
            return True, block_source

        blanket_source = self.next_line_suppressions.get((content_line, None))
        if blanket_source is not None:
            return True, f"line {blanket_source}: mermaid-check-ignore-next-line"

        category_source = self.next_line_suppressions.get((content_line, category))
        if category_source is not None:
            return True, f"line {category_source}: mermaid-check-ignore-next-line {category}"

        return False, None

    def is_block_suppressed(self, category: str) -> tuple[bool, str | None]:
        """Check whether category is suppressed for the entire block."""
        if self.block_suppress_all is not None:
            return True, f"line {self.block_suppress_all}: mermaid-check-ignore"

        source = self.block_suppressions.get(category)
        if source is not None:
            return True, f"line {source}: mermaid-check-ignore {category}"

        return False, None


def parse_suppressions(content: str) -> SuppressionSet:
    """Parse Mermaid style suppression comments from diagram content."""
    suppressions = SuppressionSet()

    for line_number, raw_line in enumerate(content.split("\n"), start=1):
        stripped = raw_line.rstrip("\r").strip()
        if not stripped.startswith("%%"):
            continue

        comment = stripped[2:].strip()
        if comment == _IGNORE_NEXT:
            suppressions.next_line_suppressions[(line_number + 1, None)] = line_number
        elif comment.startswith(f"{_IGNORE_NEXT} "):
            category = comment[len(_IGNORE_NEXT) :].strip()
            if category:
                suppressions.next_line_suppressions[(line_number + 1, category)] = line_number
        elif comment == _IGNORE:
            suppressions.block_suppress_all = line_number
        elif comment.startswith(f"{_IGNORE} "):
            category = comment[len(_IGNORE) :].strip()
            if category:
                suppressions.block_suppressions[category] = line_number

    return suppressions


__all__ = ["SuppressionSet", "parse_suppressions"]
