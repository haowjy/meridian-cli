from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt

from meridian.lib.markdown.types import (
    ExtractedDocument,
    ExtractedHeading,
    ExtractedLink,
    FencedBlock,
)

if TYPE_CHECKING:
    from markdown_it.token import Token

# Wikilink patterns: [[target]], [[target#anchor]], [[target|label]]
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?\s*(?:\|([^\]]+))?\]\]")

# External URL prefixes — not resolved as local links
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")

# Singleton markdown-it parser
_md = MarkdownIt()


def extract_file(path: Path) -> ExtractedDocument:
    """Extract structure from a markdown file using markdown-it-py.

    Returns an ExtractedDocument with error set if the file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ExtractedDocument(
            path=path,
            error=str(exc),
            frontmatter={},
            headings=[],
            fenced_blocks=[],
            references=[],
        )
    return extract_text(text, path)


def extract_text(text: str, path: Path) -> ExtractedDocument:
    """Extract structure from markdown text.

    Useful for testing without actual files.
    """
    frontmatter = _extract_frontmatter(text)
    body = _strip_frontmatter(text)

    # markdown-it-py returns a flat token list
    tokens = _md.parse(body)

    headings = _walk_headings(tokens)
    fenced_blocks = _walk_fences(tokens)
    references = _walk_links(tokens, path) + _walk_wikilinks(tokens, path)

    return ExtractedDocument(
        path=path,
        error=None,
        frontmatter=frontmatter,
        headings=headings,
        fenced_blocks=fenced_blocks,
        references=references,
    )


def _extract_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML frontmatter if present.

    Returns key: value pairs. Lines not matching the pattern are ignored.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}

    result: dict[str, str] = {}
    for _idx, line in enumerate(lines[1:], start=2):
        stripped = line.strip()
        if stripped == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key:
                result[key] = value
    return result


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from text before parsing."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text

    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            # Return everything after the closing ---
            return "\n".join(lines[i + 1 :])
    # No closing --- found, return original
    return text


def _walk_headings(tokens: list[Token]) -> list[ExtractedHeading]:
    """Extract headings from token stream."""
    headings: list[ExtractedHeading] = []

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.type == "heading_open":
            # Level is in the tag (h1, h2, etc.) or markup (# count)
            level = int(token.tag[1]) if token.tag.startswith("h") else 1
            line = (token.map[0] + 1) if token.map else 0

            # Next token should be inline with heading text
            text = ""
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                text = tokens[i + 1].content.strip()

            headings.append(ExtractedHeading(level=level, text=text, line=line))
        i += 1

    return headings


def _walk_fences(tokens: list[Token]) -> list[FencedBlock]:
    """Extract fenced code blocks from token stream."""
    blocks: list[FencedBlock] = []

    for token in tokens:
        if token.type == "fence":
            language = (token.info or "").strip()
            content = token.content
            start_line = (token.map[0] + 1) if token.map else 0
            blocks.append(FencedBlock(language=language, content=content, start_line=start_line))

    return blocks


def _walk_links(tokens: list[Token], path: Path) -> list[ExtractedLink]:
    """Extract markdown links and images from token stream."""
    links: list[ExtractedLink] = []
    parent_dir = path.parent

    for token in tokens:
        # Standard markdown image: ![alt](url)
        if token.type == "image":
            src_attr = token.attrGet("src")
            src = str(src_attr) if src_attr is not None else ""
            alt_attr = token.attrGet("alt")
            alt = str(alt_attr) if alt_attr is not None else token.content or ""
            line = (token.map[0] + 1) if token.map else 0
            resolved = _resolve_target(src, parent_dir)
            links.append(
                ExtractedLink(kind="image", target=src, text=alt, line=line, resolved=resolved)
            )

        # Walk inline children for link_open
        if token.type == "inline" and token.children:
            links.extend(_walk_inline_links(token.children, parent_dir, token))

    return links


def _walk_inline_links(
    children: list[Token], parent_dir: Path, parent_token: Token
) -> list[ExtractedLink]:
    """Extract links from inline token children."""
    links: list[ExtractedLink] = []
    i = 0

    while i < len(children):
        child = children[i]

        if child.type == "link_open":
            href_attr = child.attrGet("href")
            href = str(href_attr) if href_attr is not None else ""
            # Collect link text from subsequent tokens until link_close
            text_parts: list[str] = []
            j = i + 1
            while j < len(children) and children[j].type != "link_close":
                if children[j].type in ("text", "code_inline"):
                    text_parts.append(children[j].content)
                j += 1
            text = "".join(text_parts)
            line = (parent_token.map[0] + 1) if parent_token.map else 0

            # Strip anchor from target for resolution
            target_without_anchor = href.split("#")[0] if "#" in href else href
            resolved = _resolve_target(target_without_anchor, parent_dir)

            links.append(
                ExtractedLink(kind="link", target=href, text=text, line=line, resolved=resolved)
            )
            i = j + 1  # Skip past link_close
        else:
            i += 1

    return links


def _walk_wikilinks(tokens: list[Token], path: Path) -> list[ExtractedLink]:
    """Extract [[wikilink]] patterns from inline text tokens.

    Skips content inside fenced code blocks and inline code spans.
    """
    links: list[ExtractedLink] = []

    for token in tokens:
        if token.type == "fence":
            continue

        if token.type == "inline" and token.children:
            line = (token.map[0] + 1) if token.map else 0
            for child in token.children:
                if child.type == "code_inline":
                    continue
                if child.type == "text" and child.content:
                    for match in _WIKILINK_RE.finditer(child.content):
                        target_match = match.group(1)
                        label_match = match.group(2)
                        target = target_match.strip() if target_match else ""
                        text = label_match.strip() if label_match else target
                        links.append(
                            ExtractedLink(
                                kind="wikilink",
                                target=target,
                                text=text,
                                line=line,
                                resolved=None,  # Wikilinks don't resolve to filesystem paths
                            )
                        )

    return links


def _resolve_target(target: str, parent_dir: Path) -> Path | None:
    """Resolve a link target to an absolute path.

    Returns None for external links (http://, https://, mailto:, #).
    """
    if not target or any(target.startswith(prefix) for prefix in _EXTERNAL_PREFIXES):
        return None

    try:
        resolved = (parent_dir / target).resolve()
        return resolved
    except (OSError, ValueError):
        return None


__all__ = [
    "extract_file",
    "extract_text",
]
