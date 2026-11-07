from importlib import import_module
from typing import Any


class DeferredUnixModule:
    """Lazy module proxy so Unix-only modules load only on demand."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: Any | None = None

    def _resolve(self) -> Any:
        if self._module is None:
            self._module = import_module(self._module_name)
        return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


fcntl = DeferredUnixModule("fcntl")
termios = DeferredUnixModule("termios")
pty = DeferredUnixModule("pty")
select = DeferredUnixModule("select")
tty = DeferredUnixModule("tty")

__all__ = [
    "DeferredUnixModule",
    "fcntl",
    "pty",
    "select",
    "termios",
    "tty",
]
