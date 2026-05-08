"""Architecture-check lane runner.

Runs source-drift and architecture contract checks separately from the broad
behavioral pytest lane.
"""

from __future__ import annotations

import argparse
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
