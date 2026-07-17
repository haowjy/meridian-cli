"""OpenCode process fakes that preserve connection death ordering."""

from __future__ import annotations


class FakeOpenCodeProcess:
    """Minimal process fake whose non-zero exit is observable before event EOF."""

    def __init__(self) -> None:
        self.pid = 9001
        self.returncode: int | None = None

    def exit(self, return_code: int) -> None:
        self.returncode = return_code

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode
