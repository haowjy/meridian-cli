from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class TemplateVariableError(ValueError):
    """Template substitution failed due to undefined or malformed variables."""


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

BLOCKED_SUFFIXES: tuple[str, ...] = (".egg-info", ".pyc", ".pyo")

DEFAULT_MAX_TREE_DEPTH: int = 3

DEFAULT_MAX_TREE_ENTRIES: int = 500

MAX_FILE_SIZE_BYTES: int = 100 * 1024

BINARY_DETECTION_SAMPLE_SIZE: int = 8192


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


def is_binary_file(path: Path, *, sample_size: int = BINARY_DETECTION_SAMPLE_SIZE) -> bool:
    try:
        with path.open("rb") as f:
            sample = f.read(sample_size)
        return b"\x00" in sample
    except OSError:
        return False


def _is_blocked_dir(name: str) -> bool:
    lower = name.lower()
    if lower in BLOCKED_DIRS:
        return True
    return any(lower.endswith(suffix) for suffix in BLOCKED_SUFFIXES)


def _is_blocked_file(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in BLOCKED_SUFFIXES)


def generate_directory_tree(
    root: Path,
    *,
    max_depth: int = DEFAULT_MAX_TREE_DEPTH,
    max_entries: int = DEFAULT_MAX_TREE_ENTRIES,
) -> tuple[str, str | None]:
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
                raise
            return

        filtered: list[Path] = []
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if _is_blocked_dir(name):
                    entry_count += 1
                    if entry_count > max_entries:
                        truncated = True
                        return
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
                # Directory symlinks can loop or escape the reference root.
                lines.append(f"{prefix}{connector}{name}/ -> [symlink]")
            elif entry.is_dir():
                if _is_blocked_dir(name):
                    lines.append(f"{prefix}{connector}{name}/  (blocked)")
                elif depth >= max_depth:
                    lines.append(f"{prefix}{connector}{name}/")
                else:
                    lines.append(f"{prefix}{connector}{name}/")
                    walk(entry, child_prefix, depth + 1)
            else:
                lines.append(f"{prefix}{connector}{name}")

    root_name = root.name or root.as_posix()
    lines.append(f"{root_name}/")
    walk(root, "", 1)

    warning = None
    if truncated:
        remaining = max(0, entry_count - max_entries)
        warning = f"Tree truncated: {remaining}+ more entries"

    tree_text = "\n".join(lines)
    return tree_text, warning


def parse_template_assignments(assignments: Sequence[str]) -> dict[str, str]:

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


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes // 1024}KB"
    else:
        return f"{size_bytes // (1024 * 1024)}MB"


def _load_file_reference(path: Path) -> ReferenceItem:
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

    if is_binary_file(path):
        return ReferenceItem(
            kind="file",
            path=path,
            body="",
            warning=f"Binary file: {_format_size(size)}",
        )

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


def _resolve_reference_path(
    *,
    raw_path: str | Path,
    reference_anchor: Path,
    kb_dir: Path | None,
) -> Path:
    raw_text = str(raw_path)
    if raw_text.startswith("kb:"):
        if kb_dir is None:
            raise ValueError("Cannot resolve a kb: reference without project identity.")
        relative = raw_text[3:]
        if not relative:
            raise ValueError("Path after 'kb:' must not be empty.")
        relative_path = Path(relative)
        if relative_path.is_absolute():
            raise ValueError(f"Path after 'kb:' must be relative, got: {relative}")
        resolved = (kb_dir / relative_path).resolve()
        kb_root = kb_dir.resolve()
        try:
            resolved.relative_to(kb_root)
        except ValueError as exc:
            raise ValueError(
                f"kb: path escapes KB directory.\n  Resolved: {resolved}\n  KB root:  {kb_root}"
            ) from exc
        return resolved
    if raw_text.startswith("@"):
        replacement = raw_text[1:]
        raise ValueError(
            "The '@' prefix for reference files is no longer supported.\n"
            f"Use 'kb:{replacement}' instead."
        )

    path_obj = raw_path if isinstance(raw_path, Path) else Path(raw_text)
    expanded = path_obj.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (reference_anchor / expanded).resolve()


def _ensure_reference_exists(
    *,
    resolved_path: Path,
    raw_path: str | Path,
    reference_anchor: Path,
) -> None:
    if resolved_path.exists():
        return
    raise FileNotFoundError(
        "Reference path not found.\n"
        f"  Given: {raw_path}\n"
        f"  Reference anchor: {reference_anchor}\n"
        f"  Resolved path: {resolved_path}"
    )


def load_reference_items(
    paths: Sequence[str | Path],
    *,
    reference_anchor: Path,
    kb_dir: Path | None,
) -> tuple[ReferenceItem, ...]:
    anchor = reference_anchor.expanduser().resolve()
    resolved_kb_dir = kb_dir.expanduser().resolve() if kb_dir is not None else None
    loaded: list[ReferenceItem] = []

    for raw_path in paths:
        resolved = _resolve_reference_path(
            raw_path=raw_path,
            reference_anchor=anchor,
            kb_dir=resolved_kb_dir,
        )
        _ensure_reference_exists(
            resolved_path=resolved,
            raw_path=raw_path,
            reference_anchor=anchor,
        )

        if resolved.is_dir():
            loaded.append(_load_directory_reference(resolved))
        else:
            loaded.append(_load_file_reference(resolved))

    return tuple(loaded)


def validate_reference_paths(
    paths: Sequence[str | Path],
    *,
    reference_anchor: Path,
    kb_dir: Path | None,
) -> tuple[Path, ...]:
    anchor = reference_anchor.expanduser().resolve()
    resolved_kb_dir = kb_dir.expanduser().resolve() if kb_dir is not None else None
    validated: list[Path] = []

    for raw_path in paths:
        resolved = _resolve_reference_path(
            raw_path=raw_path,
            reference_anchor=anchor,
            kb_dir=resolved_kb_dir,
        )
        _ensure_reference_exists(
            resolved_path=resolved,
            raw_path=raw_path,
            reference_anchor=anchor,
        )
        validated.append(resolved)

    return tuple(validated)


def measure_rendered_reference_block_bytes(
    reference: ReferenceItem, *, include_warning_only: bool = True
) -> int:
    body = reference.body.strip()

    path_str = reference.path.as_posix()
    if reference.kind == "directory" and not path_str.endswith("/"):
        path_str = f"{path_str}/"

    if reference.warning:
        if body:
            block = f"# Reference: {path_str}\n\n[{reference.warning}]\n\n{body}"
            return len(block.encode("utf-8"))
        else:
            # Warning-only text is metadata, not content for context-drain warnings.
            if not include_warning_only:
                return 0
            block = f"# Reference: {path_str}\n\n[{reference.warning}]"
            return len(block.encode("utf-8"))

    if not body:
        return 0
    block = f"# Reference: {path_str}\n\n{body}"
    return len(block.encode("utf-8"))


def render_reference_blocks(references: Sequence[ReferenceItem]) -> tuple[str, ...]:
    blocks: list[str] = []
    for reference in references:
        body = reference.body.strip()

        path_str = reference.path.as_posix()
        if reference.kind == "directory" and not path_str.endswith("/"):
            path_str = f"{path_str}/"

        if reference.warning:
            if body:
                block = f"# Reference: {path_str}\n\n[{reference.warning}]\n\n{body}"
            else:
                block = f"# Reference: {path_str}\n\n[{reference.warning}]"
            blocks.append(block)
            continue

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
    "measure_rendered_reference_block_bytes",
    "parse_template_assignments",
    "render_reference_blocks",
    "resolve_template_variables",
    "substitute_template_variables",
    "validate_reference_paths",
]
