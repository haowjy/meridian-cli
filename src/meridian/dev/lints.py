"""Structural rules about the source tree, checked before the tests run.

These rules read sources instead of exercising them: they parse or scan files
under ``src/`` and report where a rule about the tree is broken. They run as the
``lints`` step of the preflight gate (see `preflight.py`) and in
`scripts/preflight.sh extended`.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.env_registry import is_registered_env_name
from meridian.lib.state.paths import LaunchPaths

# The `src/` directory holding the `meridian` package.
DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[2]

_PACKAGE_PREFIX = "meridian/"
_HARNESS_PREFIX = "meridian/lib/harness/"

# Vendored dependency trees are not ours.
_SKIPPED_PARTS = frozenset({"node_modules", "dist"})
_SCRIPT_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})


@dataclass(frozen=True, order=True)
class Violation:
    """One broken rule, with the path relative to the scanned source root."""

    path: str
    line: int
    message: str


@dataclass(frozen=True)
class PythonSource:
    """A Python module under the scanned root, parsed when a rule needs it."""

    path: str
    text: str

    def parse(self) -> ast.Module:
        return ast.parse(self.text, filename=self.path)

    def mentions(self, *markers: str) -> bool:
        return any(marker in self.text for marker in markers)


@dataclass(frozen=True)
class ScriptSource:
    """A JS/TS source whose `MERIDIAN_*` literals are plain text."""

    path: str
    text: str


@dataclass(frozen=True)
class Sources:
    """Everything the rules read, loaded once per run."""

    python_modules: tuple[PythonSource, ...]
    scripts: tuple[ScriptSource, ...]


Check = Callable[[Sources], list[Violation]]


def read_sources(source_root: Path) -> Sources:
    modules: list[PythonSource] = []
    scripts: list[ScriptSource] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(source_root)
        if _SKIPPED_PARTS.intersection(relative_path.parts):
            continue
        as_posix = relative_path.as_posix()
        if path.suffix == ".py":
            modules.append(PythonSource(as_posix, path.read_text(encoding="utf-8")))
        elif path.suffix in _SCRIPT_SUFFIXES:
            scripts.append(ScriptSource(as_posix, path.read_text(encoding="utf-8")))
    return Sources(tuple(modules), tuple(scripts))


# --- raw file writes -------------------------------------------------------

# Each exception names the narrow function that owns the legitimate raw write.
# Values are required documentation, rather than data used by the check.
RAW_WRITE_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "meridian/lib/telemetry/maintenance.py",
        "_update_marker",
        "write_text",
    ): "Best-effort cooldown marker: only its mtime matters; it is not authoritative state.",
}

# A rule only parses the modules that spell its construct: the text of a module
# cannot miss a node its AST contains. This keeps the scan proportional to the
# files that can break the rule, rather than to the whole package.
_RAW_WRITE_MARKERS = ("write_text", "write_bytes", "json.dump")
_IDLE_IMPORT_MARKER = "idle"
_ENV_LITERAL_MARKER = "MERIDIAN_"
_EXPORT_LIST_MARKER = "__all__"


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


def _raw_writes(module: PythonSource) -> list[RawWrite]:
    visitor = _RawWriteVisitor(module.path)
    visitor.visit(module.parse())
    return visitor.writes


def raw_write_violations(sources: Sources) -> list[Violation]:
    """Reject file replacement that bypasses the atomic write contract."""

    writes = [
        write
        for module in sources.python_modules
        if module.path.startswith(_PACKAGE_PREFIX) and module.mentions(*_RAW_WRITE_MARKERS)
        for write in _raw_writes(module)
    ]
    return [
        Violation(
            write.path,
            write.line,
            f"raw {write.call}() bypasses the atomic write contract: route "
            "authoritative runtime state through its lib/state wrapper, other internal "
            "file replacement through meridian.lib.platform.atomic.atomic_write_text, and "
            "plugin-facing code through meridian.plugin_api.fs.atomic_write_text; add an "
            "allowlist entry only for a non-authoritative write, with a justification.",
        )
        for write in writes
        if (write.path, write.function, write.call) not in RAW_WRITE_ALLOWLIST
    ]


def stale_raw_write_allowlist(sources: Sources) -> list[Violation]:
    """Reject allowlist entries that no longer excuse a raw write."""

    modules = {module.path: module for module in sources.python_modules}
    found: set[tuple[str, str, str]] = set()
    for path in {path for path, _function, _call in RAW_WRITE_ALLOWLIST}:
        module = modules.get(path)
        if module is None:
            continue
        found.update((write.path, write.function, write.call) for write in _raw_writes(module))
    return [
        Violation(path, 0, f"stale raw-write allowlist entry {function}.{call}: delete it")
        for path, function, call in sorted(RAW_WRITE_ALLOWLIST.keys() - found)
    ]


# --- environment variable registry -----------------------------------------
_ENV_NAME = re.compile(r"_?MERIDIAN_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
_SCRIPT_ENV_STRING = re.compile(r"""["'](_?MERIDIAN_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)["']""")
_SCRIPT_ENV_DOT_ACCESS = re.compile(
    r"""process\s*\.\s*env\s*\.\s*(_?MERIDIAN_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)\b"""
)


def _is_all_assignment(node: ast.AST) -> bool:
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return False
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets)


def _python_env_literals(module: PythonSource) -> list[tuple[str, int]]:
    tree = module.parse()
    # `__all__` re-exports a name the defining module already registers.
    exported: set[int] = (
        {
            id(child)
            for node in ast.walk(tree)
            if _is_all_assignment(node)
            for child in ast.walk(node)
            if isinstance(child, ast.Constant)
        }
        if module.mentions(_EXPORT_LIST_MARKER)
        else set()
    )
    return [
        (node.value, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in exported
        and _ENV_NAME.fullmatch(node.value)
    ]


def _script_env_literals(text: str) -> list[tuple[str, int]]:
    matches = [*_SCRIPT_ENV_STRING.finditer(text), *_SCRIPT_ENV_DOT_ACCESS.finditer(text)]
    return [(match.group(1), text.count("\n", 0, match.start()) + 1) for match in matches]


def unregistered_env_violations(sources: Sources) -> list[Violation]:
    """Reject `MERIDIAN_*` literals the env registry does not own."""

    literals = [
        (module.path, name, line)
        for module in sources.python_modules
        if module.mentions(_ENV_LITERAL_MARKER)
        for name, line in _python_env_literals(module)
    ] + [
        (script.path, name, line)
        for script in sources.scripts
        for name, line in _script_env_literals(script.text)
    ]
    return [
        Violation(
            path,
            line,
            f"{name} is not registered in meridian.env_registry: register the name there "
            "before using it.",
        )
        for path, name, line in literals
        if not is_registered_env_name(name)
    ]


# --- harness layering ------------------------------------------------------
def _imports_idle_policy(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name == "meridian.lib.idle" or alias.name.startswith("meridian.lib.idle.")
            for alias in node.names
        )
    if node.module is None:
        return False
    return (
        node.module == "meridian.lib.idle"
        or node.module.startswith("meridian.lib.idle.")
        or (node.level == 2 and node.module == "idle")
        or (node.level == 2 and node.module.startswith("idle."))
        or (node.module == "meridian.lib" and any(alias.name == "idle" for alias in node.names))
    )


def harness_idle_import_violations(sources: Sources) -> list[Violation]:
    """Reject harness adapters that import idle policy."""

    violations: list[Violation] = []
    for module in sources.python_modules:
        if not module.path.startswith(_HARNESS_PREFIX):
            continue
        if not module.mentions(_IDLE_IMPORT_MARKER):
            continue
        for node in ast.walk(module.parse()):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and _imports_idle_policy(node):
                violations.append(
                    Violation(
                        module.path,
                        node.lineno,
                        "harness code may not import meridian.lib.idle: idle policy belongs "
                        "to the launcher that owns the harness process.",
                    )
                )
    return violations


# --- per-launch paths ------------------------------------------------------
# Per-launch files are named, and launches constructed, only in `paths.py`: the
# spawn directory layout has one owner. A path built from a name held in a variable
# (`d / name`) is invisible to a literal check; the construction rule is what keeps a
# spawn directory from being wrapped as a launch. A file name `LaunchPaths` lacks is
# invisible too: add the property first.
_LAUNCH_PATHS_OWNERS = frozenset(
    {
        "meridian/lib/state/paths.py",
        "meridian/pi_runtime/extensions/shared/pi_state_paths.ts",
    }
)
_RESERVE_OWNER = "meridian/lib/state/spawn_store.py"
_PREVIEW_CALLERS = frozenset(
    {"meridian/lib/ops/spawn/prepare.py", "meridian/lib/launch/__init__.py"}
)
# This module reads `LaunchPaths` to learn the names and spells the retired identifier.
_LAUNCH_RULES_SELF = "meridian/dev/lints.py"
_PATHS_MODULE = "meridian.lib.state.paths"
_RETIRED_IDENTIFIER = "resolve_spawn_log_dir"
_LAUNCH_STRUCTURE_MARKERS = ("LaunchPaths", "replace", _PATHS_MODULE)
_SCRIPT_QUOTED = re.compile(r"""(["'`])([^"'`\n]*)\1""")


def per_launch_names() -> frozenset[str]:
    """Every file name `LaunchPaths` produces.

    The retired `history.jsonl` is not gated: Claude's own config dir has a file of the
    same name that `claude_sessions` reads.
    """
    probe = LaunchPaths.preview(Path("/probe"))
    return frozenset(
        value.name
        for attr in dir(LaunchPaths)
        if isinstance(getattr(LaunchPaths, attr), property)
        and isinstance(value := getattr(probe, attr), Path)
        and attr != "dir"
    )


def _is_per_launch_literal(value: str, names: frozenset[str]) -> bool:
    return (
        value in names
        or value.endswith(".signal")
        or any(value.endswith(f"/{name}") for name in names)
    )


def _is_launch_paths(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "LaunchPaths") or (
        isinstance(node, ast.Attribute) and node.attr == "LaunchPaths"
    )


def _is_launch_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and (
        node.id in {"launch", "launch_paths"} or node.id.endswith("_launch")
    )


def _launch_structure(module: PythonSource) -> list[tuple[int, str]]:
    """Constructions, aliases, copies, restricted calls, and private-name imports."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(module.parse()):
        if isinstance(node, ast.Call):
            target = node.func
            if _is_launch_paths(target):
                found.append(
                    (node.lineno, "LaunchPaths(...) is constructed in state/paths.py only")
                )
            is_replace = (isinstance(target, ast.Name) and target.id == "replace") or (
                isinstance(target, ast.Attribute) and target.attr == "replace"
            )
            if is_replace and node.args and _is_launch_name(node.args[0]):
                found.append((node.lineno, "a launch is copied; only its publisher assigns n"))
            if isinstance(target, ast.Attribute) and _is_launch_paths(target.value):
                if target.attr == "reserved" and module.path != _RESERVE_OWNER:
                    found.append(
                        (node.lineno, "LaunchPaths.reserved(...) is spawn_store's: it assigns n")
                    )
                if target.attr == "preview" and module.path not in _PREVIEW_CALLERS:
                    found.append(
                        (node.lineno, "LaunchPaths.preview() is for the dry-run entry points only")
                    )
        elif isinstance(node, ast.Assign) and _is_launch_paths(node.value):
            found.append(
                (node.lineno, "LaunchPaths is aliased; construct nothing outside state/paths.py")
            )
        elif isinstance(node, ast.ImportFrom) and node.module == _PATHS_MODULE:
            found.extend(
                (node.lineno, f"{alias.name} is private to paths.py; use LAUNCH_FILE_NAMES")
                for alias in node.names
                if alias.name.startswith("_")
            )
    return found


def _python_string_literals(module: PythonSource) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.value)
        for node in ast.walk(module.parse())
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _script_string_literals(script: ScriptSource) -> list[tuple[int, str]]:
    return [
        (number, match.group(2))
        for number, line in enumerate(script.text.splitlines(), start=1)
        for match in _SCRIPT_QUOTED.finditer(line)
    ]


def launch_path_violations(sources: Sources) -> list[Violation]:
    """Reject per-launch file names and `LaunchPaths` construction outside their owner."""

    names = per_launch_names()
    literal_markers = (".signal", *names)
    found: list[Violation] = []

    def literal(path: str, line: int, value: str) -> Violation:
        return Violation(
            path, line, f"{value!r} is a per-launch file; reach it through LaunchPaths"
        )

    for module in sources.python_modules:
        if module.path in _LAUNCH_PATHS_OWNERS or module.path == _LAUNCH_RULES_SELF:
            continue
        if module.mentions(_RETIRED_IDENTIFIER):
            found.append(
                Violation(module.path, 0, f"{_RETIRED_IDENTIFIER} is gone; use LaunchPaths")
            )
        if module.mentions(*literal_markers):
            found.extend(
                literal(module.path, line, value)
                for line, value in _python_string_literals(module)
                if _is_per_launch_literal(value, names)
            )
        if module.mentions(*_LAUNCH_STRUCTURE_MARKERS):
            found.extend(
                Violation(module.path, line, message) for line, message in _launch_structure(module)
            )
    for script in sources.scripts:
        if script.path in _LAUNCH_PATHS_OWNERS or script.path.endswith((".test.ts", ".d.ts")):
            continue
        if _RETIRED_IDENTIFIER in script.text:
            found.append(
                Violation(script.path, 0, f"{_RETIRED_IDENTIFIER} is gone; use LaunchPaths")
            )
        found.extend(
            literal(script.path, line, value)
            for line, value in _script_string_literals(script)
            if _is_per_launch_literal(value, names)
        )
    return found


CHECKS: tuple[Check, ...] = (
    raw_write_violations,
    stale_raw_write_allowlist,
    unregistered_env_violations,
    harness_idle_import_violations,
    launch_path_violations,
)


def collect(source_root: Path) -> list[Violation]:
    """Return every violation in the tree, in a stable order."""

    sources = read_sources(source_root)
    return sorted(violation for check in CHECKS for violation in check(sources))


def _display(source_root: Path, violation: Violation) -> str:
    path = Path(os.path.relpath(source_root / violation.path, Path.cwd())).as_posix()
    return f"{path}:{violation.line}: {violation.message}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    args = parser.parse_args(argv)
    violations = collect(args.source_root)
    for violation in violations:
        print(_display(args.source_root, violation), file=sys.stderr)
    if not violations:
        return 0
    print(f"lints: {len(violations)} violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
