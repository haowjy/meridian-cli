"""Markdown rendering, link annotation, and file serving for qi explore."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from markdown_it import MarkdownIt

from meridian.qi_explorer.graph_api import GraphIndex, is_external_link, resolve_local_target

LinkCategory = Literal["cross-ref", "source-ref", "external", "broken"]

_MD_RENDERER = MarkdownIt("commonmark", {"html": False, "linkify": True})
_A_TAG_RE = re.compile(
    r"<a\s+([^>]*?)href=(['\"])(.*?)\2([^>]*)>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class FileServeResult:
    """Result of resolving and reading a scan-root-namespaced file."""

    path: str
    content: str
    kind: Literal["markdown", "source", "binary", "not-found"]
    forbidden: bool = False


def categorize_link(
    href: str,
    *,
    source_file: Path,
    index: GraphIndex,
) -> tuple[LinkCategory, str | None, str | None]:
    """Classify a link target for annotation attributes."""

    if is_external_link(href):
        return "external", None, None

    target_without_anchor = href.split("#", 1)[0]
    if not target_without_anchor:
        return "external", None, None

    resolved = (source_file.parent / target_without_anchor).resolve()
    resolution = resolve_local_target(resolved, index)
    return resolution.category, resolution.node_id, resolution.file_id


def render_markdown_html(source_file: Path, index: GraphIndex) -> str:
    """Render markdown to annotated HTML for the content panel."""

    try:
        text = source_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    rendered = _MD_RENDERER.render(text)

    def replace_anchor(match: re.Match[str]) -> str:
        before = match.group(1)
        quote = match.group(2)
        href = match.group(3)
        after = match.group(4)
        category, node_id, file_id = categorize_link(
            href,
            source_file=source_file,
            index=index,
        )
        classes = f'class="qi-link qi-link--{category}"'
        attrs = [classes, f'data-category="{html.escape(category, quote=True)}"']
        if category == "cross-ref" and node_id is not None:
            attrs.append(f'data-node-id="{html.escape(node_id, quote=True)}"')
        if category == "source-ref" and file_id is not None:
            attrs.append(f'data-file="{html.escape(file_id, quote=True)}"')
        attr_text = " ".join(attrs)
        return f"<a {attr_text} {before}href={quote}{href}{quote}{after}>"

    return _A_TAG_RE.sub(replace_anchor, rendered)


def build_content_payload(node_id: str, index: GraphIndex) -> dict[str, object] | None:
    """Build ``GET /api/content`` JSON for one node."""

    node = index.nodes_by_id.get(node_id)
    if node is None:
        return None

    agents_html = (
        render_markdown_html(node.agents_path, index) if node.agents_path is not None else None
    )
    context_html = (
        render_markdown_html(node.context_path, index)
        if node.context_path is not None
        else None
    )

    return {
        "id": node.id,
        "label": node.label,
        "relPath": node.rel_path,
        "agentsHtml": agents_html,
        "contextHtml": context_html,
        "inboundFrom": list(index.inbound_from.get(node_id, [])),
    }


def _parse_file_id(file_id: str) -> tuple[str, str] | None:
    if ":" not in file_id:
        return None
    root_name, rel_path = file_id.split(":", 1)
    if not root_name or not rel_path:
        return None
    return root_name, rel_path


def serve_file(file_id: str, index: GraphIndex) -> FileServeResult:
    """Resolve and read a scan-root-namespaced file id."""

    parsed = _parse_file_id(file_id)
    if parsed is None:
        return FileServeResult(path=file_id, content="", kind="not-found", forbidden=True)

    root_name, rel_path = parsed
    scan_root = index.roots_by_name.get(root_name)
    if scan_root is None:
        return FileServeResult(path=file_id, content="", kind="not-found", forbidden=True)

    candidate = (scan_root / Path(rel_path)).resolve()
    try:
        candidate.relative_to(scan_root.resolve())
    except ValueError:
        return FileServeResult(path=file_id, content="", kind="not-found", forbidden=True)

    if not candidate.exists() or not candidate.is_file():
        return FileServeResult(path=file_id, content="", kind="not-found")

    try:
        with candidate.open("rb") as handle:
            sample = handle.read(512)
    except OSError:
        return FileServeResult(path=file_id, content="", kind="not-found")

    if b"\x00" in sample:
        return FileServeResult(path=file_id, content="(binary file)", kind="binary")

    if candidate.suffix.lower() == ".md":
        return FileServeResult(
            path=file_id,
            content=render_markdown_html(candidate, index),
            kind="markdown",
        )

    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return FileServeResult(path=file_id, content="", kind="not-found")

    return FileServeResult(
        path=file_id,
        content=html.escape(text),
        kind="source",
    )


__all__ = [
    "FileServeResult",
    "build_content_payload",
    "categorize_link",
    "render_markdown_html",
    "serve_file",
]
