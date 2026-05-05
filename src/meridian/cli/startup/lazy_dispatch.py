"""Lazy command dispatch helpers for startup-cheap Cyclopts assembly."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any, cast


def _resolve_lazy_target(lazy_target: str) -> Callable[..., Any]:
    """Import and resolve ``module.path:attribute.path`` to a callable."""

    module_path, separator, function_path = lazy_target.partition(":")
    if not separator or not module_path or not function_path:
        raise ValueError(
            f"lazy target must use 'module.path:function.path' format: {lazy_target!r}"
        )

    target: object = importlib.import_module(module_path)
    for part in function_path.split("."):
        if not part:
            raise ValueError(f"lazy target contains an empty path segment: {lazy_target!r}")
        target = getattr(target, part)

    if not callable(target):
        raise TypeError(f"lazy target does not resolve to a callable: {lazy_target!r}")
    return cast("Callable[..., Any]", target)


def make_lazy_command(lazy_target: str) -> Callable[..., Any]:
    """Return a callable that imports and delegates to ``lazy_target`` on invocation.

    ``lazy_target`` format is ``module.path:function_name`` or
    ``module.path:group.function_name``. Importing is intentionally deferred until
    the returned callable is invoked by Cyclopts.
    """

    def _lazy_dispatch(*args: Any, **kwargs: Any) -> Any:
        handler = _resolve_lazy_target(lazy_target)
        return handler(*args, **kwargs)

    _lazy_dispatch.__name__ = lazy_target.rsplit(":", maxsplit=1)[-1].replace(".", "_")
    _lazy_dispatch.__qualname__ = _lazy_dispatch.__name__
    _lazy_dispatch.__doc__ = f"Lazy dispatcher for {lazy_target}."
    return _lazy_dispatch


__all__ = ["make_lazy_command"]
