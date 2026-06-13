"""Discover qi explore scan roots from a primary path and meridian context config."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meridian.lib.bootstrap.project_state import load_context_config
from meridian.lib.config.context_config import ContextConfig
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.context.resolver import context_env_key, resolve_context_paths

ScanRootKind = Literal["primary", "context"]

PRIMARY_ROOT_NAME = "codebase"


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


def _env_path(env_key: str) -> Path | None:
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _append_root(
    roots: list[ScanRoot],
    seen: set[Path],
    *,
    name: str,
    path: Path,
    kind: ScanRootKind,
) -> None:
    resolved = path.resolve()
    if not resolved.is_dir():
        return
    if resolved in seen:
        return
    seen.add(resolved)
    roots.append(ScanRoot(name=name, abs_path=resolved, kind=kind))


def discover_scan_roots(primary: Path) -> DiscoveryResult:
    """Build ordered scan roots: primary codebase plus existing context dirs."""

    primary_resolved = primary.resolve()
    roots: list[ScanRoot] = []
    seen: set[Path] = set()

    _append_root(
        roots,
        seen,
        name=PRIMARY_ROOT_NAME,
        path=primary_resolved,
        kind="primary",
    )

    project_root = resolve_project_root_resolution(
        execution_cwd=primary_resolved,
    ).project_root
    context_config = load_context_config(project_root) or ContextConfig()
    resolved = resolve_context_paths(project_root, context_config)

    kb_path = _env_path(context_env_key("kb")) or resolved.kb_root
    _append_root(roots, seen, name="kb", path=kb_path, kind="context")

    work_path = _env_path("MERIDIAN_ACTIVE_WORK_DIR") or _env_path(
        context_env_key("work")
    )
    if work_path is None:
        work_path = resolved.work_root
    _append_root(roots, seen, name="work", path=work_path, kind="context")

    strategy_path = _env_path(context_env_key("strategy"))
    if strategy_path is None and "strategy" in resolved.extra:
        strategy_path = resolved.extra["strategy"][0]
    if strategy_path is not None:
        _append_root(roots, seen, name="strategy", path=strategy_path, kind="context")

    for name in sorted(resolved.extra):
        if name == "strategy":
            continue
        env_path = _env_path(context_env_key(name))
        path = env_path if env_path is not None else resolved.extra[name][0]
        _append_root(roots, seen, name=name, path=path, kind="context")

    return DiscoveryResult(primary=primary_resolved, roots=roots)


__all__ = [
    "PRIMARY_ROOT_NAME",
    "DiscoveryResult",
    "ScanRoot",
    "ScanRootKind",
    "discover_scan_roots",
]
