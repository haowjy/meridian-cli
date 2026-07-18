"""Document extraction types for markdown parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

LinkKind = Literal["link", "image", "wikilink"]


@dataclass
class ExtractedLink:
    """One link extracted from a markdown document."""

    kind: LinkKind
    target: str  # raw URL or wikilink target (anchor stripped for links)
    text: str  # display text or alt
    line: int  # 1-indexed line in source file
    resolved: Path | None  # None for external, wikilinks; Path for local links


@dataclass
class ExtractedHeading:
    """One heading extracted from a markdown document."""

    level: int  # 1-6
    text: str  # stripped text content
    line: int


@dataclass
class FencedBlock:
    """One fenced code block extracted from a markdown document."""

    language: str  # empty string if no language tag
    content: str  # raw block content, no fence lines
    start_line: int


@dataclass
class ExtractedDocument:
    """Complete extraction result for one markdown file."""

    path: Path
    error: str | None  # set if file couldn't be read
    frontmatter: dict[str, str] = field(default_factory=dict[str, str])  # raw key: value pairs
    headings: list[ExtractedHeading] = field(default_factory=list[ExtractedHeading])
    fenced_blocks: list[FencedBlock] = field(default_factory=list[FencedBlock])
    references: list[ExtractedLink] = field(default_factory=list[ExtractedLink])


__all__ = [
    "ExtractedDocument",
    "ExtractedHeading",
    "ExtractedLink",
    "FencedBlock",
    "LinkKind",
]
