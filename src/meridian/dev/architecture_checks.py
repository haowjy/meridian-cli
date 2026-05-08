"""Architecture-check lane runner.

Runs source-drift and architecture contract checks separately from the broad
behavioral pytest lane.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArchitectureCheck:
    """One architecture drift invariant enforced by the architecture-check lane."""

    check_id: str
    description: str
    run: Callable[[Path], list[str]]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _source_lines(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return list(enumerate(text.splitlines(), start=1))


def _search(path: Path, pattern: str) -> list[tuple[int, str]]:
    compiled = re.compile(pattern)
    return [(lineno, line) for lineno, line in _source_lines(path) if compiled.search(line)]


@dataclass(frozen=True)
class _PlatformBoundaryWaiver:
    """Explicitly approved module-level PLAT-04 exception."""

    path: str
    rationale: str


_PLAT04_APPROVED_PREFIXES: tuple[str, ...] = (
    # Capability adapters that intentionally isolate OS-specific imports/branches.
    "src/meridian/lib/platform/",
)
_PLAT04_APPROVED_WAIVERS: tuple[_PlatformBoundaryWaiver, ...] = (
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/launch/process/pty_launcher.py",
        rationale="posix-only PTY launch mechanism boundary",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/launch/process/windows_launcher.py",
        rationale="windows console launch mechanism boundary",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/launch/signals.py",
        rationale="signal semantics adapter",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/state/user_paths.py",
        rationale="user-state-root authority boundary",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/state/atomic.py",
        rationale="cross-platform atomic-write mechanism",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/state/session_store.py",
        rationale="session-store lock and file-mode mechanics",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/streaming/control_socket.py",
        rationale="control-channel transport boundary",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/streaming/signal_canceller.py",
        rationale="cancellation transport boundary",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/plugin_api/fs.py",
        rationale="plugin file-lock primitive boundary",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/cli/spawn_inject.py",
        rationale="CLI bridge for transport discovery path selection",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/harness/claude_preflight.py",
        rationale="harness-specific launch preflight behavior",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/harness/codex.py",
        rationale="harness-specific shell/env behavior",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/safety/guardrails.py",
        rationale="cross-platform shell safety boundary",
    ),
    _PlatformBoundaryWaiver(
        path="src/meridian/lib/ops/spawn/execute.py",
        rationale="runtime transport selection bridge",
    ),
)
_PLAT04_APPROVED_WAIVER_PATHS = frozenset(waiver.path for waiver in _PLAT04_APPROVED_WAIVERS)
_PLAT04_PLATFORM_MODULES = frozenset({"os", "sys", "platform", "meridian.lib.platform"})
_PLAT04_PLATFORM_SPECIFIC_MODULES = frozenset({"fcntl", "pty", "termios", "msvcrt", "winreg"})


def _is_plat04_approved(relative_path: str) -> bool:
    return relative_path.startswith(_PLAT04_APPROVED_PREFIXES) or (
        relative_path in _PLAT04_APPROVED_WAIVER_PATHS
    )


def _line_text(lines: Sequence[str], lineno: int) -> str:
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def _plat04_violation(*, relative_path: str, lineno: int, name: str, line: str) -> str:
    return (
        f"{relative_path}:{lineno}: PLAT-04 platform boundary drift — "
        f"unapproved {name} usage in non-adapter module: {line}"
    )


def _check_platform_boundary_drift_in_file(path: Path, *, relative_path: str) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    source_lines = text.splitlines()
    tree = ast.parse(text, filename=relative_path)

    module_aliases: dict[str, str] = {}
    imported_symbol_aliases: dict[str, str] = {}
    platform_system_aliases: set[str] = set()
    banned_import_hits: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name in _PLAT04_PLATFORM_MODULES:
                    module_aliases[local_name] = alias.name
                if alias.name in _PLAT04_PLATFORM_SPECIFIC_MODULES:
                    banned_import_hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if module is None:
                continue
            if module in _PLAT04_PLATFORM_SPECIFIC_MODULES:
                banned_import_hits.extend((node.lineno, f"import {module}") for _ in node.names)
                continue
            if module == "sys":
                for alias in node.names:
                    if alias.name == "platform":
                        imported_symbol_aliases[alias.asname or alias.name] = "sys.platform"
            elif module == "os":
                for alias in node.names:
                    if alias.name == "name":
                        imported_symbol_aliases[alias.asname or alias.name] = "os.name"
            elif module == "platform":
                for alias in node.names:
                    if alias.name == "system":
                        platform_system_aliases.add(alias.asname or alias.name)
            elif module == "meridian.lib.platform":
                for alias in node.names:
                    if alias.name == "*":
                        banned_import_hits.append((node.lineno, "IS_WINDOWS"))
                    elif alias.name == "IS_WINDOWS":
                        banned_import_hits.append((node.lineno, "IS_WINDOWS"))
                        imported_symbol_aliases[alias.asname or alias.name] = "IS_WINDOWS"

    hits: list[tuple[int, str]] = []
    hits.extend(banned_import_hits)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name):
                continue
            module_name = module_aliases.get(node.value.id)
            if module_name == "os" and node.attr == "name":
                hits.append((node.lineno, "os.name"))
            elif module_name == "sys" and node.attr == "platform":
                hits.append((node.lineno, "sys.platform"))
            elif module_name == "meridian.lib.platform" and node.attr == "IS_WINDOWS":
                hits.append((node.lineno, "IS_WINDOWS"))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            symbol = imported_symbol_aliases.get(node.id)
            if symbol is not None:
                hits.append((node.lineno, symbol))
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                module_name = module_aliases.get(node.func.value.id)
                if module_name == "platform" and node.func.attr == "system":
                    hits.append((node.lineno, "platform.system()"))
            elif isinstance(node.func, ast.Name) and node.func.id in platform_system_aliases:
                hits.append((node.lineno, "platform.system()"))

    unique_hits = sorted(set(hits))
    return [
        _plat04_violation(
            relative_path=relative_path,
            lineno=lineno,
            name=name,
            line=_line_text(source_lines, lineno),
        )
        for lineno, name in unique_hits
    ]


def _check_platform_boundary_drift(project_root: Path) -> list[str]:
    source_root = project_root / "src"
    this_module = Path(__file__).resolve()

    violations: list[str] = []
    for path in _iter_python_files(source_root):
        if path.resolve() == this_module:
            continue

        relative = path.relative_to(project_root).as_posix()
        if _is_plat04_approved(relative):
            continue

        violations.extend(_check_platform_boundary_drift_in_file(path, relative_path=relative))
    return violations


def _check_spawn_store_lifecycle_transitions(project_root: Path) -> list[str]:
    source_root = project_root / "src/meridian/lib"
    allowed_callsite = source_root / "core/lifecycle.py"
    ignored_definer = source_root / "state/spawn_store.py"
    transition_pattern = re.compile(
        r"spawn_store\.(start_spawn|mark_spawn_running|record_spawn_exited|finalize_spawn|mark_finalizing)\("
    )

    violations: list[str] = []
    for path in _iter_python_files(source_root):
        if path in {allowed_callsite, ignored_definer}:
            continue
        for lineno, line in _source_lines(path):
            if transition_pattern.search(line):
                violations.append(
                    f"{path.relative_to(project_root).as_posix()}:{lineno}: "
                    f"spawn_store lifecycle transition outside core/lifecycle.py — {line.strip()}"
                )
    return violations


def _check_resolved_context_entrypoints(project_root: Path) -> list[str]:
    source_root = project_root / "src/meridian/lib"
    allowed = {
        "src/meridian/lib/core/context.py",
        "src/meridian/lib/launch/context.py",
        "src/meridian/lib/ops/context.py",
    }

    hits: dict[str, list[int]] = {}
    for path in _iter_python_files(source_root):
        relative = path.relative_to(project_root).as_posix()
        line_hits = [
            lineno
            for lineno, line in _source_lines(path)
            if "ResolvedContext.from_environment(" in line
        ]
        if line_hits:
            hits[relative] = line_hits

    unexpected = sorted(set(hits) - allowed)
    missing = sorted(allowed - set(hits))

    violations: list[str] = []
    for relative in unexpected:
        for lineno in hits[relative]:
            violations.append(
                f"{relative}:{lineno}: unexpected ResolvedContext.from_environment() entrypoint"
            )
    for relative in missing:
        violations.append(
            f"{relative}:missing: expected ResolvedContext.from_environment() entrypoint not found"
        )
    return violations


def _check_banned_symbol_absent(
    project_root: Path,
    *,
    root: Path,
    symbol: str,
    scope_label: str,
    excluded_paths: frozenset[Path] = frozenset(),
) -> list[str]:
    pattern = rf"\b{re.escape(symbol)}\b"
    violations: list[str] = []
    for path in _iter_python_files(root):
        if path.resolve() in excluded_paths:
            continue
        for lineno, line in _search(path, pattern):
            violations.append(
                f"{path.relative_to(project_root).as_posix()}:{lineno}: {scope_label} contains "
                f"banned symbol {symbol} — {line.strip()}"
            )
    return violations


def _check_launch_banned_dtos(project_root: Path) -> list[str]:
    source_root = project_root / "src"
    tests_root = project_root / "tests"
    this_module = Path(__file__).resolve()

    violations: list[str] = []
    for symbol in (
        "PreparedSpawnPlan",
        "ExecutionPolicy",
        "SessionContinuation",
        "ResolvedPrimaryLaunchPlan",
    ):
        violations.extend(
            _check_banned_symbol_absent(
                project_root,
                root=source_root,
                symbol=symbol,
                scope_label="source",
                excluded_paths=frozenset({this_module}),
            )
        )

    violations.extend(
        _check_banned_symbol_absent(
            project_root,
            root=tests_root,
            symbol="ResolvedPrimaryLaunchPlan",
            scope_label="tests",
            excluded_paths=frozenset({this_module}),
        )
    )
    return violations


def _check_streaming_executor_boundary(project_root: Path) -> list[str]:
    source_root = project_root / "src"
    executor_path = source_root / "meridian/lib/ops/spawn/execute.py"

    forbidden: tuple[tuple[str, str], ...] = (
        ("resolve_permission_pipeline", r"resolve_permission_pipeline\s*\("),
        ("TieredPermissionResolver()", r"\bTieredPermissionResolver\s*\("),
        ("UnsafeNoOpPermissionResolver()", r"\bUnsafeNoOpPermissionResolver\s*\("),
        ("adapter.fork_session()", r"\.fork_session\s*\("),
        ("adapter.seed_session()", r"\.seed_session\s*\("),
        ("adapter.resolve_launch_spec()", r"\.resolve_launch_spec\s*\("),
        ("adapter.build_command()", r"\.build_command\s*\("),
        ("build_harness_child_env()", r"\bbuild_harness_child_env\s*\("),
    )

    if not executor_path.exists():
        return []

    violations: list[str] = []
    for name, pattern in forbidden:
        for lineno, line in _search(executor_path, pattern):
            stripped = line.strip()
            if stripped.startswith(("from ", "import ")):
                continue
            violations.append(
                f"{executor_path.relative_to(project_root).as_posix()}:{lineno}: "
                f"streaming executor calls forbidden composition function {name} — {stripped}"
            )
    return violations


def _check_executor_mechanism_boundary(project_root: Path) -> list[str]:
    source_root = project_root / "src"
    executors = (
        source_root / "meridian/lib/launch/process/__init__.py",
        source_root / "meridian/lib/launch/process/runner.py",
        source_root / "meridian/lib/launch/streaming_runner.py",
    )
    forbidden: tuple[tuple[str, str], ...] = (
        ("resolve_policies()", r"\bresolve_policies\s*\("),
        ("resolve_permission_pipeline()", r"\bresolve_permission_pipeline\s*\("),
        ("TieredPermissionResolver()", r"\bTieredPermissionResolver\s*\("),
        ("adapter.build_command()", r"\.build_command\s*\("),
        ("adapter.seed_session()", r"\.seed_session\s*\("),
    )

    violations: list[str] = []
    for path in executors:
        if not path.exists():
            continue
        for name, pattern in forbidden:
            for lineno, line in _search(path, pattern):
                stripped = line.strip()
                if stripped.startswith(("from ", "import ")):
                    continue
                violations.append(
                    f"{path.relative_to(project_root).as_posix()}:{lineno}: "
                    f"executor performs forbidden composition {name} — {stripped}"
                )
    return violations


def _check_deleted_placeholder_modules(project_root: Path) -> list[str]:
    deleted_paths = (
        project_root / "src/meridian/lib/launch/runner.py",
        project_root / "src/meridian/lib/ops/spawn/plan.py",
    )

    violations: list[str] = []
    for path in deleted_paths:
        if path.exists():
            violations.append(
                f"{path.relative_to(project_root).as_posix()}:1: "
                "expected deleted module reintroduced"
            )
    return violations


CHECKS: tuple[ArchitectureCheck, ...] = (
    ArchitectureCheck(
        check_id="LIFECYCLE-01",
        description="spawn_store lifecycle transitions only route through lifecycle service",
        run=_check_spawn_store_lifecycle_transitions,
    ),
    ArchitectureCheck(
        check_id="CONTEXT-01",
        description="ResolvedContext.from_environment entrypoints stay scoped to approved modules",
        run=_check_resolved_context_entrypoints,
    ),
    ArchitectureCheck(
        check_id="LAUNCH-DTO-01",
        description="deleted launch DTO names stay absent from source and load-bearing tests",
        run=_check_launch_banned_dtos,
    ),
    ArchitectureCheck(
        check_id="LAUNCH-BOUNDARY-01",
        description="streaming executor avoids composition callsites",
        run=_check_streaming_executor_boundary,
    ),
    ArchitectureCheck(
        check_id="LAUNCH-BOUNDARY-02",
        description="launch executors remain mechanism-only",
        run=_check_executor_mechanism_boundary,
    ),
    ArchitectureCheck(
        check_id="LAUNCH-BOUNDARY-03",
        description="deleted placeholder modules remain deleted",
        run=_check_deleted_placeholder_modules,
    ),
    ArchitectureCheck(
        check_id="PLAT-04",
        description="platform boundary drift remains scoped to approved adapter modules",
        run=_check_platform_boundary_drift,
    ),
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="architecture-check",
        description="Run architecture/source-drift checks outside behavioral pytest.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available architecture checks and exit.",
    )
    parser.add_argument(
        "--check",
        action="append",
        dest="selected_checks",
        metavar="CHECK_ID",
        help="Run only the specified check ID (repeatable).",
    )
    return parser


def _selected_checks(check_ids: Sequence[str] | None) -> tuple[ArchitectureCheck, ...]:
    if not check_ids:
        return CHECKS

    unique_ids = list(dict.fromkeys(check_ids))
    by_id = {check.check_id: check for check in CHECKS}
    missing = sorted(check_id for check_id in unique_ids if check_id not in by_id)
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"Unknown architecture check id(s): {names}")

    return tuple(by_id[check_id] for check_id in unique_ids)


def run_checks(checks: Sequence[ArchitectureCheck], *, project_root: Path | None = None) -> int:
    root = _project_root() if project_root is None else project_root
    failed = 0

    for check in checks:
        violations = sorted(check.run(root))
        if not violations:
            print(f"PASS {check.check_id} {check.description}")
            continue

        failed += 1
        print(f"FAIL {check.check_id} {check.description}")
        for violation in violations:
            print(f"  - {violation}")

    if failed:
        print(f"architecture-check: {failed}/{len(checks)} checks failed")
        return 1

    print(f"architecture-check: all {len(checks)} checks passed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.list:
        for check in CHECKS:
            print(f"{check.check_id}\t{check.description}")
        return 0

    try:
        checks = _selected_checks(args.selected_checks)
    except ValueError as exc:
        parser.error(str(exc))

    return run_checks(checks)


if __name__ == "__main__":
    raise SystemExit(main())
