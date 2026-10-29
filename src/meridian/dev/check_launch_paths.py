"""Source gate: per-launch files are named, and launches constructed, only in ``paths.py``.

Fails when, outside ``state/paths.py`` (Python) and ``shared/pi_state_paths.ts`` (Pi):

- a string literal is, or ends with ``/``, a per-launch file name (``"spawns/p1/report.md"``,
  ``f"{d}/report.md"``), or ends with ``.signal``;
- ``LaunchPaths(...)`` is constructed, aliased, or copied with ``replace(launch, ...)``
  (``for_row`` in ``paths.py`` is the only way to a published launch);
- ``LaunchPaths.reserved(...)`` is called outside ``state/spawn_store.py``, the code that
  assigns ``n``;
- ``LaunchPaths.preview()`` is called outside the two dry-run entry points
  (``ops/spawn/prepare.py``, ``launch/__init__.py``): a preview names no real spawn;
- a private name (``_X``) is imported from ``paths.py`` (code that recognizes a per-launch
  name uses ``LAUNCH_FILE_NAMES``);
- the retired ``resolve_spawn_log_dir`` reappears.

Test files are not scanned. A path built from a name held in a variable
(``d / name``) is invisible to a literal check; the construction rule is what keeps a
spawn directory from being wrapped as a launch. A file name ``LaunchPaths`` lacks is
invisible too: add the property first.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

from meridian.lib.state.paths import LaunchPaths

_SRC = Path(__file__).resolve().parents[1]
_ALLOWED = {
    _SRC / "lib" / "state" / "paths.py",
    _SRC / "pi_runtime" / "extensions" / "shared" / "pi_state_paths.ts",
}
_RESERVE_ALLOWED = _SRC / "lib" / "state" / "spawn_store.py"
_PREVIEW_ALLOWED = {
    _SRC / "lib" / "ops" / "spawn" / "prepare.py",
    _SRC / "lib" / "launch" / "__init__.py",
}
_PATHS_MODULE = "meridian.lib.state.paths"
_RETIRED_IDENTIFIER = "resolve_spawn_log_dir"


def per_launch_names() -> frozenset[str]:
    """Every file name ``LaunchPaths`` produces.

    The retired ``history.jsonl`` is not gated: Claude's own config dir has a file of the
    same name that ``claude_sessions`` reads.
    """
    probe = LaunchPaths.preview(Path("/probe"))
    names = {
        value.name
        for attr in dir(LaunchPaths)
        if isinstance(getattr(LaunchPaths, attr), property)
        and isinstance(value := getattr(probe, attr), Path)
        and attr != "dir"
    }
    return frozenset(names)


def _is_per_launch_literal(value: str, names: frozenset[str]) -> bool:
    return (
        value in names
        or value.endswith(".signal")
        or any(value.endswith(f"/{name}") for name in names)
    )


def _python_literals(path: Path) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _is_launch_paths(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "LaunchPaths") or (
        isinstance(node, ast.Attribute) and node.attr == "LaunchPaths"
    )


def _is_launch_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and (
        node.id in {"launch", "launch_paths"} or node.id.endswith("_launch")
    )


def _python_structure(path: Path) -> Iterator[tuple[int, str]]:
    """Constructions, aliases, copies, restricted calls, and private-name imports."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            target = node.func
            if _is_launch_paths(target):
                yield node.lineno, "LaunchPaths(...) is constructed in state/paths.py only"
            is_replace = (isinstance(target, ast.Name) and target.id == "replace") or (
                isinstance(target, ast.Attribute) and target.attr == "replace"
            )
            if is_replace and node.args and _is_launch_name(node.args[0]):
                yield node.lineno, "a launch is copied; only its publisher assigns n"
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "reserved"
                and _is_launch_paths(target.value)
                and path != _RESERVE_ALLOWED
            ):
                yield node.lineno, "LaunchPaths.reserved(...) is spawn_store's: it assigns n"
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "preview"
                and _is_launch_paths(target.value)
                and path not in _PREVIEW_ALLOWED
            ):
                yield node.lineno, "LaunchPaths.preview() is for the dry-run entry points only"
        elif isinstance(node, ast.Assign) and _is_launch_paths(node.value):
            yield node.lineno, "LaunchPaths is aliased; construct nothing outside state/paths.py"
        elif isinstance(node, ast.ImportFrom) and node.module == _PATHS_MODULE:
            for alias in node.names:
                if alias.name.startswith("_"):
                    yield (
                        node.lineno,
                        f"{alias.name} is private to paths.py; use LAUNCH_FILE_NAMES",
                    )


def _typescript_literals(path: Path) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for match in re.finditer(r"""(["'`])([^"'`\n]*)\1""", line):
            yield number, match.group(2)


def violations(src: Path = _SRC) -> list[str]:
    names = per_launch_names()
    found: list[str] = []
    for path in sorted(src.rglob("*")):
        if path in _ALLOWED or "node_modules" in path.parts or path.suffix not in {".py", ".ts"}:
            continue
        if path.name.endswith((".test.ts", ".d.ts")) or path.name == Path(__file__).name:
            continue
        relative = path.relative_to(src).as_posix()
        if _RETIRED_IDENTIFIER in path.read_text(encoding="utf-8"):
            found.append(f"{relative}: {_RETIRED_IDENTIFIER} is gone; use LaunchPaths")
        literals = _python_literals(path) if path.suffix == ".py" else _typescript_literals(path)
        found.extend(
            f"{relative}:{line}: {value!r} is a per-launch file; reach it through LaunchPaths"
            for line, value in literals
            if _is_per_launch_literal(value, names)
        )
        if path.suffix == ".py":
            found.extend(
                f"{relative}:{line}: {message}" for line, message in _python_structure(path)
            )
    return found


def main(argv: Sequence[str] | None = None) -> int:
    _ = argv
    found = violations()
    for message in found:
        print(message, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
