"""Ops module for `meridian qi` — inline knowledge navigation."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.util import FormatContext

# Directories to skip during discovery scans.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".meridian",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".agents",
    }
)


class QiKnowledgePoint(BaseModel):
    """One discovered AGENTS.md or .context/CONTEXT.md location."""

    model_config = ConfigDict(frozen=True)

    rel_path: str  # root-relative, forward slashes
    kind: str  # "agents" or "context"


class QiListOutput(BaseModel):
    """Output for `meridian qi list`."""

    model_config = ConfigDict(frozen=True)

    root: str
    points: list[QiKnowledgePoint]

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.points:
            return f"No inline knowledge found under {self.root}"
        lines: list[str] = [f"Inline knowledge under {self.root}:", ""]
        for point in self.points:
            tag = "[agents]" if point.kind == "agents" else "[context]"
            lines.append(f"  {tag}  {point.rel_path}")
        return "\n".join(lines)


class QiShowOutput(BaseModel):
    """Output for `meridian qi <path>` and bare `meridian qi`."""

    model_config = ConfigDict(frozen=True)

    boundary_path: str  # root-relative path to the knowledge boundary directory
    agents_content: str | None  # content of AGENTS.md if found
    context_content: str | None  # content of .context/CONTEXT.md if found

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        parts: list[str] = []
        if self.agents_content is not None:
            parts.append(f"# {self.boundary_path}/AGENTS.md")
            parts.append("")
            parts.append(self.agents_content)
        if self.context_content is not None:
            if parts:
                parts.append("")
            parts.append(f"# {self.boundary_path}/.context/CONTEXT.md")
            parts.append("")
            parts.append(self.context_content)
        if not parts:
            return f"No inline knowledge found at boundary: {self.boundary_path}"
        return "\n".join(parts)


def discover_knowledge_points(root: Path) -> list[QiKnowledgePoint]:
    """Walk the tree under *root* and return all AGENTS.md / .context/CONTEXT.md files."""
    import os

    points: list[QiKnowledgePoint] = []

    def _rel(p: Path) -> str:
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return p.as_posix()

    for dirpath_str, dirnames, filenames in os.walk(root):
        dirpath = Path(dirpath_str)
        # Prune skipped directories in-place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        if "AGENTS.md" in filenames:
            points.append(
                QiKnowledgePoint(rel_path=_rel(dirpath / "AGENTS.md"), kind="agents")
            )

        # .context/CONTEXT.md — only care if the subdir is present.
        if ".context" in dirnames or ".context" in filenames:
            context_file = dirpath / ".context" / "CONTEXT.md"
            if context_file.is_file():
                points.append(
                    QiKnowledgePoint(
                        rel_path=_rel(context_file),
                        kind="context",
                    )
                )

    return points


def find_boundary(path: Path) -> Path | None:
    """Walk up from *path* to find the nearest enclosing knowledge boundary directory.

    A directory qualifies as a boundary when it contains AGENTS.md or
    .context/CONTEXT.md.

    Returns the boundary directory or None when nothing is found.
    """
    # Start from the directory itself; if given a file, start from its parent.
    candidate = path if path.is_dir() else path.parent
    candidate = candidate.resolve()

    while True:
        if _is_knowledge_boundary(candidate):
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def _is_knowledge_boundary(directory: Path) -> bool:
    """Return True if *directory* has AGENTS.md or .context/CONTEXT.md."""
    return (directory / "AGENTS.md").is_file() or (
        directory / ".context" / "CONTEXT.md"
    ).is_file()


class QiCheckFinding(BaseModel):
    """One structural health finding."""

    model_config = ConfigDict(frozen=True)

    # "missing_context_md" | "orphan_context" | "broken_link" | "missing_agents" | "unreadable"
    category: str
    severity: str  # "error" or "warning"
    file: str  # root-relative path to the file with the issue
    line: int  # 1-indexed line number (0 if not line-specific)
    message: str
    detail: str | None = None  # e.g., the broken link target


class QiCheckOutput(BaseModel):
    """Output for `meridian qi check`."""

    model_config = ConfigDict(frozen=True)

    root: str
    findings: list[QiCheckFinding]
    points_scanned: int

    @property
    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.findings:
            return f"All checks passed ({self.points_scanned} points scanned)"
        lines: list[str] = []
        for finding in self.findings:
            prefix = "ERROR" if finding.severity == "error" else "WARN "
            loc = f"{finding.file}:{finding.line}" if finding.line else finding.file
            lines.append(f"  [{prefix}]  {loc}  {finding.message}")
            if finding.detail:
                lines.append(f"           → {finding.detail}")
        lines.append(f"\n{self.error_count} error(s), {self.warning_count} warning(s)")
        return "\n".join(lines)


def qi_list_sync(root: Path) -> QiListOutput:
    """Synchronous handler for `meridian qi list`."""
    points = discover_knowledge_points(root)
    return QiListOutput(root=root.as_posix(), points=points)


def qi_show_sync(path: Path, project_root: Path) -> QiShowOutput:
    """Synchronous handler for `meridian qi <path>` / bare `meridian qi`."""

    resolved = path.resolve()
    boundary = find_boundary(resolved)

    if boundary is None:
        # Fall back to the resolved path itself so we can show an informative message.
        boundary = resolved if resolved.is_dir() else resolved.parent

    def _rel(p: Path) -> str:
        try:
            return p.relative_to(project_root).as_posix()
        except ValueError:
            return p.as_posix()

    agents_content: str | None = None
    agents_file = boundary / "AGENTS.md"
    if agents_file.is_file():
        agents_content = agents_file.read_text(encoding="utf-8")

    context_content: str | None = None
    context_file = boundary / ".context" / "CONTEXT.md"
    if context_file.is_file():
        context_content = context_file.read_text(encoding="utf-8")

    return QiShowOutput(
        boundary_path=_rel(boundary),
        agents_content=agents_content,
        context_content=context_content,
    )


def qi_check_sync(root: Path) -> QiCheckOutput:
    """Synchronous handler for `meridian qi check`."""
    import os

    from meridian.lib.markdown.extract import extract_file

    findings: list[QiCheckFinding] = []
    points_scanned = 0

    def _rel(p: Path) -> str:
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return p.as_posix()

    def _check_links(md_file: Path, is_agents: bool) -> None:
        """Extract links from *md_file* and emit findings for broken ones."""
        doc = extract_file(md_file)
        if doc.error is not None:
            findings.append(
                QiCheckFinding(
                    category="unreadable",
                    severity="error",
                    file=_rel(md_file),
                    line=0,
                    message=f"Could not read file: {doc.error}",
                )
            )
            return
        for ref in doc.references:
            # Skip external links and wikilinks (no filesystem path to check)
            if ref.kind == "wikilink" or ref.resolved is None:
                continue
            if ref.resolved.exists():
                continue
            # Broken link — categorise by whether it targets the *local sibling*
            # .context/CONTEXT.md (same directory as this AGENTS.md).
            # A link to a non-sibling .context/CONTEXT.md is still a broken_link error.
            is_context_ref = False
            if is_agents:
                local_context = md_file.parent / ".context" / "CONTEXT.md"
                try:
                    is_context_ref = ref.resolved == local_context.resolve()
                except OSError:
                    is_context_ref = ref.resolved == local_context
            if is_context_ref:
                findings.append(
                    QiCheckFinding(
                        category="missing_agents",
                        severity="warning",
                        file=_rel(md_file),
                        line=ref.line,
                        message="AGENTS.md references missing .context/CONTEXT.md",
                        detail=ref.target,
                    )
                )
            else:
                findings.append(
                    QiCheckFinding(
                        category="broken_link",
                        severity="error",
                        file=_rel(md_file),
                        line=ref.line,
                        message=f"Broken link: {ref.target}",
                        detail=ref.target,
                    )
                )

    for dirpath_str, dirnames, filenames in os.walk(root):
        dirpath = Path(dirpath_str)
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        # Check .context/ directory health
        if ".context" in dirnames:
            context_dir = dirpath / ".context"
            context_file = context_dir / "CONTEXT.md"
            if not context_file.is_file():
                findings.append(
                    QiCheckFinding(
                        category="missing_context_md",
                        severity="error",
                        file=_rel(context_dir),
                        line=0,
                        message=".context/ directory has no CONTEXT.md",
                    )
                )
            else:
                points_scanned += 1
                # orphan_context: CONTEXT.md present but no sibling AGENTS.md
                if "AGENTS.md" not in filenames:
                    findings.append(
                        QiCheckFinding(
                            category="orphan_context",
                            severity="warning",
                            file=_rel(context_file),
                            line=0,
                            message=".context/CONTEXT.md has no sibling AGENTS.md",
                        )
                    )
                _check_links(context_file, is_agents=False)

        # Check AGENTS.md health
        if "AGENTS.md" in filenames:
            agents_file = dirpath / "AGENTS.md"
            points_scanned += 1
            _check_links(agents_file, is_agents=True)

    # Sort: errors before warnings, then by file path, then by line
    findings.sort(key=lambda f: (0 if f.severity == "error" else 1, f.file, f.line))

    return QiCheckOutput(
        root=root.as_posix(),
        findings=findings,
        points_scanned=points_scanned,
    )


__all__ = [
    "QiCheckFinding",
    "QiCheckOutput",
    "QiKnowledgePoint",
    "QiListOutput",
    "QiShowOutput",
    "discover_knowledge_points",
    "find_boundary",
    "qi_check_sync",
    "qi_list_sync",
    "qi_show_sync",
]
