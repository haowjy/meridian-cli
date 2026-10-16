"""Alias-aware call counting for state I/O cost contracts."""

from __future__ import annotations

import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest


@dataclass(frozen=True)
class ProbeCall:
    """One observed call and the named locks held when it began."""

    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]
    held_locks: frozenset[str]


class IOProbe:
    """Count calls through every loaded Meridian alias of an I/O function."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self._calls: dict[str, list[ProbeCall]] = {}
        self._held_locks: list[str] = []
        self._lock_paths: dict[Path, str] = {}
        self._lock_target: Callable[..., Any] | None = None

    def watch(self, name: str, target: Callable[..., Any]) -> None:
        """Record calls made through every loaded module alias of ``target``.

        Modules imported after ``watch`` are not patched.
        """
        if name in self._calls:
            raise ValueError(f"Probe already watches {name!r}")
        self._calls[name] = []

        @wraps(target)
        def observed(*args: Any, **kwargs: Any) -> Any:
            self._calls[name].append(
                ProbeCall(args, dict(kwargs), frozenset(self._held_locks))
            )
            return target(*args, **kwargs)

        self._patch_loaded_aliases(target, observed)

    def watch_lock(
        self,
        name: str,
        target: Callable[..., Any],
        path: Path,
    ) -> None:
        """Name a lock path so watched calls can report when it is held."""
        resolved_path = Path(path)
        if resolved_path in self._lock_paths:
            raise ValueError(f"Probe already names lock path {resolved_path}")
        self._lock_paths[resolved_path] = name
        if self._lock_target is not None:
            if self._lock_target is not target:
                raise ValueError("One IOProbe cannot watch multiple lock functions")
            return
        self._lock_target = target

        @contextmanager
        @wraps(target)
        def observed_lock(*args: Any, **kwargs: Any) -> Generator[Any, None, None]:
            lock_name = self._lock_name(args, kwargs)
            with target(*args, **kwargs) as handle:
                if lock_name is not None:
                    self._held_locks.append(lock_name)
                try:
                    yield handle
                finally:
                    if lock_name is not None:
                        removed = self._held_locks.pop()
                        assert removed == lock_name

        self._patch_loaded_aliases(target, observed_lock)

    def calls(self, name: str) -> tuple[ProbeCall, ...]:
        return tuple(self._calls[name])

    def count(self, name: str) -> int:
        return len(self._calls[name])

    def calls_under_lock(
        self,
        name: str,
        lock_name: str | None = None,
    ) -> tuple[ProbeCall, ...]:
        return tuple(
            call
            for call in self._calls[name]
            if (
                bool(call.held_locks)
                if lock_name is None
                else lock_name in call.held_locks
            )
        )

    def count_under_lock(self, name: str, lock_name: str | None = None) -> int:
        return len(self.calls_under_lock(name, lock_name))

    def is_lock_held(self, name: str) -> bool:
        return name in self._held_locks

    def _lock_name(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str | None:
        candidate = args[0] if args else kwargs.get("path")
        if candidate is None:
            return None
        return self._lock_paths.get(Path(candidate))

    def _patch_loaded_aliases(
        self,
        target: Callable[..., Any],
        replacement: Callable[..., Any],
    ) -> None:
        aliases: list[tuple[ModuleType, str]] = []
        for module_name, module in tuple(sys.modules.items()):
            if not (module_name == "meridian" or module_name.startswith("meridian.")):
                continue
            aliases.extend(
                (module, attribute)
                for attribute, value in tuple(vars(module).items())
                if value is target
            )
        if not aliases:
            raise ValueError(f"No loaded Meridian aliases found for {target!r}")
        for module, attribute in aliases:
            self._monkeypatch.setattr(module, attribute, replacement)
