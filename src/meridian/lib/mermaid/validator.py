"""Mermaid diagram validation: Python default with optional JS strict mode.

Python tier: heuristic structural validation (default).
JS tier: full grammar validation via bundled @mermaid-js/parser (optional).

The Python tier catches obvious structural errors. The JS tier provides
authoritative validation when Node.js and the validator bundle are available.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from meridian.lib.mermaid.scanner import DiagramTarget, collect_targets

BUNDLE_PATH = Path(__file__).parent / "mermaid-validator.bundle.js"

# Validation timeout per block (seconds)
TIMEOUT_SECS = 10

ValidationTier = Literal["python", "js"]


class NodeNotFoundError(RuntimeError):
    """Raised when node is not available on PATH."""


class BundleNotFoundError(EnvironmentError):
    """Raised when the JS bundle is missing (packaging error)."""


@dataclass
class ValidationError:
    """Details about a validation error."""

    message: str
    line: int | None = None
    column: int | None = None


@dataclass
class BlockResult:
    """Result of validating one mermaid block."""

    file: str  # Relative to root
    line: int  # Block start line (1-indexed)
    valid: bool
    error: str | None = None
    diagram_type: str | None = None
    tier: ValidationTier = "python"


@dataclass
class ScanOptions:
    """Options for scanning files."""

    exclude: list[str] = field(default_factory=lambda: [])
    depth: int | None = None


@dataclass
class MermaidValidationResult:
    """Complete validation result for a file or directory."""

    path: str
    tier: ValidationTier
    total_blocks: int
    valid_blocks: int
    invalid_blocks: int
    has_errors: bool
    results: list[BlockResult]


def detect_tier() -> ValidationTier:
    """Detect which validation tier is available.

    Returns "js" if Node.js and the bundle are available, otherwise "python".
    No warnings are emitted - Python is the default and JS is optional.
    """
    if shutil.which("node") is None:
        return "python"
    if not BUNDLE_PATH.exists():
        return "python"
    return "js"


def validate_path(
    path: Path,
    *,
    opts: ScanOptions | None = None,
) -> MermaidValidationResult:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")

    opts = opts or ScanOptions()

    # Determine scan root.
    root = path if path.is_dir() else path.parent

    # Collect all Mermaid targets (markdown fenced blocks + standalone files).
    targets = collect_targets(
        path,
        root,
        depth=opts.depth,
        exclude=opts.exclude if opts.exclude else None,
    )

    # Detect available tier.
    tier = detect_tier()

    # Validate all blocks.
    results: list[BlockResult] = []
    for target in targets:
        result = _validate_block_js(target) if tier == "js" else _validate_block_python(target)
        results.append(result)

    valid_count = sum(1 for result in results if result.valid)
    invalid_count = len(results) - valid_count

    return MermaidValidationResult(
        path=path.as_posix(),
        tier=tier,
        total_blocks=len(results),
        valid_blocks=valid_count,
        invalid_blocks=invalid_count,
        has_errors=invalid_count > 0,
        results=results,
    )


# --------------------------------------------------------------------------
# Python tier validation (heuristic)
# --------------------------------------------------------------------------

# Known diagram types (all lowercase for matching)
DIAGRAM_TYPES: dict[str, str] = {
    "flowchart": "flowchart",
    "graph": "flowchart",
    "sequencediagram": "sequence",
    "classdiagram": "class",
    "statediagram": "state",
    "statediagram-v2": "state",
    "erdiagram": "er",
    "gantt": "gantt",
    "pie": "pie",
    "gitgraph": "git",
    "mindmap": "mindmap",
    "timeline": "timeline",
    "journey": "journey",
    "xychart-beta": "xychart",
    "sankey-beta": "sankey",
    "quadrantchart": "quadrant",
    "block-beta": "block",
    "packet-beta": "packet",
    "architecture-beta": "architecture",
}

# Block-level keywords that need matching "end"
BLOCK_OPENERS = frozenset({"loop", "alt", "opt", "par", "rect", "subgraph", "if"})


def _strip_frontmatter(content: str) -> str:
    lines = content.split("\n")
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[index + 1 :])
    return content


def _detect_diagram_type(content: str) -> tuple[str | None, str | None]:
    """Detect the diagram type from content.

    Returns (canonical_type, raw_first_token) or (None, None) if undetected.
    """
    body = _strip_frontmatter(content)
    in_directive = False

    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # Handle multi-line directives
        if in_directive:
            if stripped.endswith("}%%"):
                in_directive = False
            continue

        if stripped.startswith("%%{"):
            if not stripped.endswith("}%%"):
                in_directive = True
            continue

        if stripped.startswith("%%"):
            continue

        parts = stripped.split()
        if not parts:
            continue
        first_token = parts[0].lower()
        canonical = DIAGRAM_TYPES.get(first_token)
        if canonical:
            return canonical, parts[0]
        return None, parts[0]  # Unrecognized type

    return None, None  # Empty after preprocessing


def _validate_heuristic(content: str, _diagram_type: str | None) -> list[str]:
    body = _strip_frontmatter(content)
    errors: list[str] = []

    open_directives = 0
    block_depth = 0

    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("%%{"):
            if not stripped.endswith("}%%"):
                open_directives += 1
            continue
        if stripped.endswith("}%%") and open_directives > 0:
            open_directives -= 1
            continue

        if stripped.startswith("%%"):
            continue

        # Track block openers/closers
        parts = stripped.lower().split()
        first = parts[0] if parts else ""
        if first in BLOCK_OPENERS:
            block_depth += 1
        elif first == "end":
            block_depth = max(0, block_depth - 1)

    if open_directives > 0:
        errors.append("unclosed %%{...}%% directive")
    if block_depth > 0:
        errors.append(f"unclosed block (missing {block_depth} 'end' keyword(s))")

    return errors


def _validate_block_python(target: DiagramTarget) -> BlockResult:
    canonical, raw_token = _detect_diagram_type(target.content)

    if canonical is None:
        if raw_token is None:
            error = "no diagram type declaration found"
        else:
            error = f"unrecognized diagram type: {raw_token}"
        return BlockResult(
            file=target.rel,
            line=target.start_line,
            valid=False,
            error=error,
            diagram_type=None,
            tier="python",
        )

    errors = _validate_heuristic(target.content, canonical)
    if errors:
        return BlockResult(
            file=target.rel,
            line=target.start_line,
            valid=False,
            error="; ".join(errors),
            diagram_type=canonical,
            tier="python",
        )

    return BlockResult(
        file=target.rel,
        line=target.start_line,
        valid=True,
        error=None,
        diagram_type=canonical,
        tier="python",
    )


# --------------------------------------------------------------------------
# JS tier validation (authoritative)
# --------------------------------------------------------------------------


def _validate_block_js(target: DiagramTarget) -> BlockResult:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".mmd",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(target.content)
        tmp_path = Path(tmp.name)

    try:
        proc = subprocess.run(
            ["node", str(BUNDLE_PATH), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return BlockResult(
            file=target.rel,
            line=target.start_line,
            valid=False,
            error="validation timed out",
            diagram_type=None,
            tier="js",
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    if proc.returncode == 0:
        diagram_type: str | None = None
        try:
            parsed = json.loads(proc.stdout)
            if isinstance(parsed, dict):
                parsed_dict = cast("dict[str, object]", parsed)
                value = parsed_dict.get("diagramType")
                if isinstance(value, str):
                    diagram_type = value
        except json.JSONDecodeError:
            pass
        return BlockResult(
            file=target.rel,
            line=target.start_line,
            valid=True,
            error=None,
            diagram_type=diagram_type,
            tier="js",
        )

    error_msg: str | None = None
    diagram_type: str | None = None
    try:
        parsed = json.loads(proc.stdout)
        if isinstance(parsed, dict):
            parsed_dict = cast("dict[str, object]", parsed)
            error_value = parsed_dict.get("error")
            if isinstance(error_value, str):
                error_msg = error_value
            type_value = parsed_dict.get("diagramType")
            if isinstance(type_value, str):
                diagram_type = type_value
    except json.JSONDecodeError:
        pass

    if not error_msg:
        error_msg = proc.stdout.strip() or proc.stderr.strip() or "parse error"

    return BlockResult(
        file=target.rel,
        line=target.start_line,
        valid=False,
        error=error_msg,
        diagram_type=diagram_type,
        tier="js",
    )


__all__ = [
    "BlockResult",
    "BundleNotFoundError",
    "MermaidValidationResult",
    "NodeNotFoundError",
    "ScanOptions",
    "ValidationError",
    "ValidationTier",
    "detect_tier",
    "validate_path",
]
