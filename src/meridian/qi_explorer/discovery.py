"""Discover qi explore scan roots from primary paths and meridian context config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.bootstrap.project_state import load_context_config
from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import resolve_context_paths

ScanRootKind = Literal["primary", "context"]

PRIMARY_ROOT_NAME = "codebase"
RESERVED_CONTEXT_NAMES = frozenset({"kb", "work", "strategy"})


@dataclass(frozen=True)
class ScanRoot:
    """One filesystem root included in a qi explore session."""

    name: str
    abs_path: Path
    kind: ScanRootKind


@dataclass(frozen=True)
class DiscoveryResult:
    """Resolved scan roots for graph building and file serving."""

    primary: Path
    roots: list[ScanRoot]

    @property
    def name_to_path(self) -> dict[str, Path]:
        return {root.name: root.abs_path for root in self.roots}


def _root_label(path: Path) -> str:
    name = path.name
    if name:
        return name
    # Filesystem root (POSIX "/", Windows "D:\\") has empty .name.
    anchor = "".join(ch for ch in path.anchor if ch.isalnum())
    return anchor or "root"


def _unique_name(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def _is_meridian_project(root: Path) -> bool:
    return (root / "meridian.toml").is_file() or (root / "meridian.local.toml").is_file()


def _append_root(
    roots: list[ScanRoot],
    seen: set[Path],
    used_names: set[str],
    *,
    base_name: str,
    path: Path,
    kind: ScanRootKind,
    reserved: frozenset[str] = frozenset(),
) -> None:
    resolved = path.resolve()
    if not resolved.is_dir():
        return
    if resolved in seen:
        return
    name = _unique_name(base_name, used_names | reserved)
    used_names.add(name)
    seen.add(resolved)
    roots.append(ScanRoot(name=name, abs_path=resolved, kind=kind))


def _append_context_roots(
    roots: list[ScanRoot],
    seen: set[Path],
    used_names: set[str],
    *,
    project_root: Path,
) -> None:
    config = load_context_config(project_root) or ContextConfig()
    resolved = resolve_context_paths(project_root, config)

    _append_root(
        roots,
        seen,
        used_names,
        base_name="kb",
        path=resolved.kb_root,
        kind="context",
    )
    _append_root(
        roots,
        seen,
        used_names,
        base_name="work",
        path=resolved.work_root,
        kind="context",
    )

    if "strategy" in resolved.extra:
        _append_root(
            roots,
            seen,
            used_names,
            base_name="strategy",
            path=resolved.extra["strategy"][0],
            kind="context",
        )

    for name in sorted(resolved.extra):
        if name == "strategy":
            continue
        _append_root(
            roots,
            seen,
            used_names,
            base_name=name,
            path=resolved.extra[name][0],
            kind="context",
        )


def discover_scan_roots(primaries: list[Path]) -> DiscoveryResult:
    """Build ordered scan roots from one or more primary paths."""

    if not primaries:
        primaries = [Path(".")]

    resolved_primaries = [primary.resolve() for primary in primaries]
    roots: list[ScanRoot] = []
    seen: set[Path] = set()
    used_names: set[str] = set()

    for index, primary_path in enumerate(resolved_primaries):
        base_name = PRIMARY_ROOT_NAME if index == 0 else _root_label(primary_path)
        _append_root(
            roots,
            seen,
            used_names,
            base_name=base_name,
            path=primary_path,
            kind="primary",
            reserved=RESERVED_CONTEXT_NAMES,
        )

    for primary_path in resolved_primaries:
        if not _is_meridian_project(primary_path):
            continue
        _append_context_roots(roots, seen, used_names, project_root=primary_path)

    return DiscoveryResult(primary=resolved_primaries[0], roots=roots)


__all__ = [
    "PRIMARY_ROOT_NAME",
    "DiscoveryResult",
    "ScanRoot",
    "ScanRootKind",
    "discover_scan_roots",
]
