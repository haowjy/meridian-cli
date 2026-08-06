"""Shared output sink protocol and no-op sink."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class OutputSink(Protocol):
    def result(self, payload: Any) -> None: ...

    def status(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(
        self,
        message: str,
        *,
        exit_code: int = 1,
        name: str | None = None,
    ) -> None: ...

    def heartbeat(self, message: str) -> None: ...

    def event(self, payload: dict[str, Any]) -> None: ...


class NullSink:
    def result(self, payload: Any) -> None:
        _ = payload

    def status(self, message: str) -> None:
        _ = message

    def warning(self, message: str) -> None:
        _ = message

    def error(
        self,
        message: str,
        *,
        exit_code: int = 1,
        name: str | None = None,
    ) -> None:
        _ = (message, exit_code, name)

    def heartbeat(self, message: str) -> None:
        _ = message

    def event(self, payload: dict[str, Any]) -> None:
        _ = payload

    def flush(self) -> None:
        return
