"""Reference-file loading and template substitution helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.state.paths import resolve_kb_dir

_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class TemplateVariableError(ValueError):
    """Template substitution failed due to undefined or malformed variables."""


# -----------------------------------------------------------------------------
# Constants for directory tree rendering
# -----------------------------------------------------------------------------

#: Directories to skip completely during tree traversal.
BLOCKED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".meridian",
        ".mars",
        ".agents",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        ".gradle",
        "target",
        "coverage",
        ".hypothesis",
        ".parcel-cache",
        "out",
        "bin",
        "obj",
        "DerivedData",
        "Pods",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "htmlcov",
    }
)

#: Suffixes to skip in both tree rendering and file listing.
BLOCKED_SUFFIXES: tuple[str, ...] = (".egg-info", ".pyc", ".pyo")

#: Default maximum tree depth (3 levels from root).
DEFAULT_MAX_TREE_DEPTH: int = 3

#: Default maximum entries before truncation.
DEFAULT_MAX_TREE_ENTRIES: int = 500

#: Maximum file size for inline content (100KB).
MAX_FILE_SIZE_BYTES: int = 100 * 1024

#: Sample size for binary detection.
BINARY_DETECTION_SAMPLE_SIZE: int = 8192


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------


class ReferenceItem(BaseModel):
    """One reference item loaded from `-f` flags.

    For files: `kind='file'`, `body` contains file content.
    For directories: `kind='directory'`, `body` contains rendered tree.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["file", "directory"] = "file"
    path: Path
    body: str
    warning: str | None = None


# -----------------------------------------------------------------------------
# Binary Detection
# -----------------------------------------------------------------------------


def is_binary_file(path: Path, *, sample_size: int = BINARY_DETECTION_SAMPLE_SIZE) -> bool:
    """Check if file appears to be binary by sampling for null bytes."""
    try:
        with path.open("rb") as f:
            sample = f.read(sample_size)
        return b"\x00" in sample
    except OSError:
        return False


# -----------------------------------------------------------------------------
# Directory Tree Rendering
# -----------------------------------------------------------------------------


def _is_blocked_dir(name: str) -> bool:
    """Check if directory name should be blocked from tree traversal."""
    lower = name.lower()
    if lower in BLOCKED_DIRS:
        return True
    return any(lower.endswith(suffix) for suffix in BLOCKED_SUFFIXES)


def _is_blocked_file(name: str) -> bool:
    """Check if file should be excluded from tree output."""
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in BLOCKED_SUFFIXES)


def generate_directory_tree(
    root: Path,
    *,
    max_depth: int = DEFAULT_MAX_TREE_DEPTH,
    max_entries: int = DEFAULT_MAX_TREE_ENTRIES,
) -> tuple[str, str | None]:
    """Generate tree representation of directory structure.

    Returns:
        Tuple of (tree_string, warning_or_none).
        Tree uses box-drawing characters and depth-limited expansion.
        Directories at the depth limit appear as `name/` with no children.

    Args:
        root: Directory path to render.
        max_depth: Maximum depth to expand (default 3).
        max_entries: Maximum total entries before truncation.
    """
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    lines: list[str] = []
    entry_count = 0
    truncated = False

    def walk(dir_path: Path, prefix: str, depth: int) -> None:
        nonlocal entry_count, truncated

        if truncated:
            return

        try:
            entries = sorted(
                dir_path.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except PermissionError:
            if depth == 1:
                # Top-level reference directory must fail loud.
                raise
            return

        # Filter blocked entries
        filtered: list[Path] = []
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if _is_blocked_dir(name):
                    # Show blocked dirs with annotation
                    entry_count += 1
                    if entry_count > max_entries:
                        truncated = True
                        return
                    # We'll add this as a blocked marker below
                    filtered.append(entry)
                else:
                    filtered.append(entry)
            else:
                if not _is_blocked_file(name):
                    filtered.append(entry)

        for i, entry in enumerate(filtered):
            if truncated:
                return

            entry_count += 1
            if entry_count > max_entries:
                truncated = True
                return

            is_last = i == len(filtered) - 1
            connector = "└── " if is_last else "├── "
            child_prefix = prefix + ("    " if is_last else "│   ")
            name = entry.name

            if entry.is_symlink() and entry.is_dir():
                # Symlink to directory - don't follow
                lines.append(f"{prefix}{connector}{name}/ -> [symlink]")
            elif entry.is_dir():
                if _is_blocked_dir(name):
                    lines.append(f"{prefix}{connector}{name}/  (blocked)")
                elif depth >= max_depth:
                    # At depth limit - show as unexpanded
                    lines.append(f"{prefix}{connector}{name}/")
                else:
                    lines.append(f"{prefix}{connector}{name}/")
                    walk(entry, child_prefix, depth + 1)
            else:
                lines.append(f"{prefix}{connector}{name}")

    # Start with root directory name
    root_name = root.name or root.as_posix()
    lines.append(f"{root_name}/")
    walk(root, "", 1)

    warning = None
    if truncated:
        remaining = max(0, entry_count - max_entries)
        warning = f"Tree truncated: {remaining}+ more entries"

    tree_text = "\n".join(lines)
    return tree_text, warning


# -----------------------------------------------------------------------------
# Template Variables
# -----------------------------------------------------------------------------


def parse_template_assignments(assignments: Sequence[str]) -> dict[str, str]:
    """Parse CLI template vars passed as `KEY=VALUE`."""

    parsed: dict[str, str] = {}
    for assignment in assignments:
        key, separator, value = assignment.partition("=")
        normalized_key = key.strip()
        if not separator or not normalized_key:
            raise ValueError(
                f"Invalid template variable assignment. Expected KEY=VALUE, got '{assignment}'."
            )
        parsed[normalized_key] = value
    return parsed


def resolve_template_variables(
    variables: Mapping[str, str | Path],
    *,
    base_dir: Path | None = None,
) -> dict[str, str]:
    """Resolve template variable values (`@path`/Path -> file contents, else literal)."""

    root = (base_dir or Path.cwd()).resolve()
    resolved: dict[str, str] = {}
    for raw_key, raw_value in variables.items():
        key = raw_key.strip()
        if not key:
            raise ValueError("Template variable names must not be empty.")

        value: str | None = None
        path_candidate: Path | None = None
        if isinstance(raw_value, Path):
            path_candidate = raw_value
        else:
            candidate_text = raw_value
            if candidate_text.startswith("@"):
                path_candidate = Path(candidate_text[1:])
            else:
                value = candidate_text

        if path_candidate is not None:
            expanded = path_candidate.expanduser()
            resolved_path = (expanded if expanded.is_absolute() else root / expanded).resolve()
            if not resolved_path.is_file():
                raise FileNotFoundError(
                    f"Template variable '{key}' points to missing file: {resolved_path}"
                )
            value = resolved_path.read_text(encoding="utf-8")

        assert value is not None
        resolved[key] = value
    return resolved


def substitute_template_variables(
    text: str,
    variables: Mapping[str, str],
    *,
    strict: bool = True,
) -> str:
    """Substitute `{{KEY}}` placeholders.

    In strict mode, undefined variables raise `TemplateVariableError`.
    In non-strict mode, undefined placeholders are preserved as-is.
    """

    if strict:
        missing = sorted(
            {
                match.group(1)
                for match in _TEMPLATE_VAR_RE.finditer(text)
                if match.group(1) not in variables
            }
        )
        if missing:
            joined = ", ".join(missing)
            raise TemplateVariableError(f"Undefined template variables: {joined}")

    return _TEMPLATE_VAR_RE.sub(
        lambda match: variables.get(match.group(1), match.group(0)),
        text,
    )


# -----------------------------------------------------------------------------
# Reference Loading
# -----------------------------------------------------------------------------


def _format_size(size_bytes: int) -> str:
    """Format byte size for human display."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes // 1024}KB"
    else:
        return f"{size_bytes // (1024 * 1024)}MB"


def _load_file_reference(path: Path) -> ReferenceItem:
    """Load a single file reference with content checks."""
    try:
        stat = path.stat()
        size = stat.st_size
    except OSError as e:
        return ReferenceItem(
            kind="file",
            path=path,
            body="",
            warning=f"Cannot read file: {e}",
        )

    # Check size limit
    if size > MAX_FILE_SIZE_BYTES:
        return ReferenceItem(
            kind="file",
            path=path,
            body="",
            warning=(
                f"File too large: {_format_size(size)} > "
                f"{_format_size(MAX_FILE_SIZE_BYTES)} limit. Read from disk."
            ),
        )

    # Check if binary
    if is_binary_file(path):
        return ReferenceItem(
            kind="file",
            path=path,
            body="",
            warning=f"Binary file: {_format_size(size)}",
        )

    # Read content
    try:
        content = path.read_text(encoding="utf-8")
        return ReferenceItem(kind="file", path=path, body=content)
    except UnicodeDecodeError:
        return ReferenceItem(
            kind="file",
            path=path,
            body="",
            warning=f"Binary file: {_format_size(size)}",
        )
    except OSError as e:
        return ReferenceItem(
            kind="file",
            path=path,
            body="",
            warning=f"Cannot read file: {e}",
        )


def _load_directory_reference(path: Path) -> ReferenceItem:
    """Load a directory reference as tree representation."""
    try:
        tree, warning = generate_directory_tree(path)
        return ReferenceItem(
            kind="directory",
            path=path,
            body=tree,
            warning=warning,
        )
    except ValueError as e:
        return ReferenceItem(
            kind="directory",
            path=path,
            body="",
            warning=str(e),
        )


def load_reference_items(
    paths: Sequence[str | Path],
    *,
    base_dir: Path | None = None,
) -> tuple[ReferenceItem, ...]:
    """Load reference items (files or directories) in input order.

    Args:
        paths: Sequence of file or directory paths. Paths starting with '@'
               are resolved relative to the KB directory.
        base_dir: Base directory for relative path resolution.

    Returns:
        Tuple of ReferenceItem objects with content or tree representations.

    Raises:
        FileNotFoundError: If a path doesn't exist.
    """
    root = (base_dir or Path.cwd()).resolve()
    loaded: list[ReferenceItem] = []

    for raw_path in paths:
        # Resolve path
        if isinstance(raw_path, str) and raw_path.startswith("@"):
            relative = raw_path[1:]
            if not relative:
                raise ValueError("Reference path after '@' must not be empty.")
            resolved = (resolve_kb_dir(root) / relative).resolve()
        else:
            path_obj = raw_path if isinstance(raw_path, Path) else Path(raw_path)
            expanded = path_obj.expanduser()
            resolved = (expanded if expanded.is_absolute() else root / expanded).resolve()

        # Check existence
        if not resolved.exists():
            raise FileNotFoundError(f"Reference path not found: {resolved}")

        # Load based on type
        if resolved.is_dir():
            loaded.append(_load_directory_reference(resolved))
        else:
            loaded.append(_load_file_reference(resolved))

    return tuple(loaded)


def validate_reference_paths(
    paths: Sequence[str | Path],
    *,
    base_dir: Path | None = None,
) -> tuple[Path, ...]:
    """Validate reference paths exist without reading content.

    Args:
        paths: Sequence of file or directory paths.
        base_dir: Base directory for relative path resolution.

    Returns:
        Tuple of resolved absolute paths.

    Raises:
        FileNotFoundError: If a path doesn't exist.
    """
    root = (base_dir or Path.cwd()).resolve()
    validated: list[Path] = []

    for raw_path in paths:
        if isinstance(raw_path, str) and raw_path.startswith("@"):
            relative = raw_path[1:]
            if not relative:
                raise ValueError("Reference path after '@' must not be empty.")
            resolved = (resolve_kb_dir(root) / relative).resolve()
        else:
            path_obj = raw_path if isinstance(raw_path, Path) else Path(raw_path)
            expanded = path_obj.expanduser()
            resolved = (expanded if expanded.is_absolute() else root / expanded).resolve()

        if not resolved.exists():
            raise FileNotFoundError(f"Reference path not found: {resolved}")
        validated.append(resolved)

    return tuple(validated)


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------


def render_reference_blocks(references: Sequence[ReferenceItem]) -> tuple[str, ...]:
    """Render loaded references as isolated prompt sections.

    For files: renders content with `# Reference: <path>` header.
    For directories: renders tree with `# Reference: <path>/` header.
    """
    blocks: list[str] = []
    for reference in references:
        body = reference.body.strip()

        # Build path display - add trailing slash for directories
        path_str = reference.path.as_posix()
        if reference.kind == "directory" and not path_str.endswith("/"):
            path_str = f"{path_str}/"

        # Handle warning case
        if reference.warning:
            if body:
                block = f"# Reference: {path_str}\n\n[{reference.warning}]\n\n{body}"
            else:
                block = f"# Reference: {path_str}\n\n[{reference.warning}]"
            blocks.append(block)
            continue

        # Normal content
        if not body:
            continue
        blocks.append(f"# Reference: {path_str}\n\n{body}")

    return tuple(blocks)


__all__ = [
    "BLOCKED_DIRS",
    "BLOCKED_SUFFIXES",
    "DEFAULT_MAX_TREE_DEPTH",
    "DEFAULT_MAX_TREE_ENTRIES",
    "MAX_FILE_SIZE_BYTES",
    "ReferenceItem",
    "TemplateVariableError",
    "generate_directory_tree",
    "is_binary_file",
    "load_reference_items",
    "parse_template_assignments",
    "render_reference_blocks",
    "resolve_template_variables",
    "substitute_template_variables",
    "validate_reference_paths",
]
