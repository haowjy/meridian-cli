"""Reject raw file replacement that can corrupt file-backed authority."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "meridian"

# Each exception names the narrow function that owns the legitimate raw write.
# Values are required documentation, rather than data used by the assertion.
ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "lib/telemetry/maintenance.py",
        "_update_marker",
        "write_text",
    ): "Best-effort cooldown marker: only its mtime matters; it is not authoritative state.",
}


@dataclass(frozen=True, order=True)
class RawWrite:
    path: str
    function: str
    call: str
    line: int


class _RawWriteVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.function = "<module>"
        self.writes: list[RawWrite] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        call = _raw_write_name(node.func)
        if call is not None:
            self.writes.append(RawWrite(self.path, self.function, call, node.lineno))
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = previous


def _raw_write_name(function: ast.expr) -> str | None:
    if not isinstance(function, ast.Attribute):
        return None
    if function.attr in {"write_text", "write_bytes"}:
        return function.attr
    if (
        function.attr == "dump"
        and isinstance(function.value, ast.Name)
        and function.value.id == "json"
    ):
        return "json.dump"
    return None


def _find_raw_writes() -> list[RawWrite]:
    writes: list[RawWrite] = []
    for source_path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative_path = source_path.relative_to(SOURCE_ROOT).as_posix()
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        visitor = _RawWriteVisitor(relative_path)
        visitor.visit(tree)
        writes.extend(visitor.writes)
    return sorted(writes)


def test_authoritative_files_are_not_written_raw() -> None:
    writes = _find_raw_writes()
    found_keys = {(write.path, write.function, write.call) for write in writes}
    stale_allowlist = sorted(ALLOWLIST.keys() - found_keys)
    assert not stale_allowlist, f"Remove stale raw-write allowlist entries: {stale_allowlist}"

    violations = [
        write
        for write in writes
        if (write.path, write.function, write.call) not in ALLOWLIST
    ]

    assert not violations, (
        "Raw file writes bypass Meridian's atomic mutation contract:\n"
        + "\n".join(
            f"  {write.path}:{write.line} ({write.function}): {write.call}(...)"
            for write in violations
        )
        + "\nRoute authoritative runtime state through its lib/state wrapper; "
        "route other internal file replacement through "
        "meridian.lib.platform.atomic.atomic_write_text; plugin-facing code must use "
        "meridian.plugin_api.atomic_write_text. Add an allowlist entry only for a "
        "non-authoritative write, with a one-line justification."
    )
