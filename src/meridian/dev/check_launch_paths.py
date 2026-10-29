"""Source gate: per-launch files are named, and launches constructed, only in ``paths.py``.

Fails when, outside ``state/paths.py`` (Python) and ``shared/pi_state_paths.ts`` (Pi):

- a string literal is, or ends with ``/``, a per-launch file name (``"spawns/p1/report.md"``,
  ``f"{d}/report.md"``), or ends with ``.signal``;
- ``LaunchPaths(...)`` is constructed (use ``for_row``/``for_launch`` in ``paths.py``);
- the retired ``resolve_spawn_log_dir`` reappears.

Test files are not scanned. A path built from a name held in a variable
(``d / name``) is invisible to a literal check; the construction rule is what keeps a
spawn directory from being wrapped as a launch.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.state.paths import LaunchPaths

_SRC = Path(__file__).resolve().parents[1]
_ALLOWED = {
    _SRC / "lib" / "state" / "paths.py",
    _SRC / "pi_runtime" / "extensions" / "shared" / "pi_state_paths.ts",
}
_RETIRED_IDENTIFIER = "resolve_spawn_log_dir"


@dataclass(frozen=True)
class _ProbeRow:
    id: str


def per_launch_names() -> frozenset[str]:
    """Every file name ``LaunchPaths`` produces.

    The retired ``history.jsonl`` is not gated: Claude's own config dir has a file of the
    same name that ``claude_sessions`` reads.
    """
    probe = LaunchPaths.for_row(Path("/probe"), _ProbeRow("p1"))
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


def _python_constructions(path: Path) -> Iterator[int]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            target = node.func
            if (isinstance(target, ast.Name) and target.id == "LaunchPaths") or (
                isinstance(target, ast.Attribute) and target.attr == "LaunchPaths"
            ):
                yield node.lineno


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
                f"{relative}:{line}: LaunchPaths(...) is constructed in state/paths.py only"
                for line in _python_constructions(path)
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
