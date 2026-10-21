"""Named background lanes: latest request per lane wins, results re-enter on the UI loop."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable

type Post = Callable[[Callable[[], None]], None]


class Lanes:
    """Run blocking work on daemon threads and apply results only while still current.

    Daemon threads (not the asyncio executor) keep Application exit from waiting on a long
    transcript scan; a superseded or cancelled request never touches UI state. Work waits
    on ``ready`` so first-use imports of the cyclic ops modules happen in one thread.
    """

    def __init__(
        self, invoke: Callable[[Callable[[], None]], None], ready: threading.Event
    ) -> None:
        self._invoke = invoke
        self._ready = ready
        self._tokens: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()
        self._closed = False

    def run[T](
        self,
        lane: str,
        work: Callable[[Callable[[], bool], Post], T],
        apply: Callable[[T], None],
        fail: Callable[[str], None],
    ) -> None:
        """``work(current, post)`` may call ``post(fn)`` to publish progress mid-flight."""
        with self._lock:
            self._tokens[lane] += 1
            token = self._tokens[lane]

        def current() -> bool:
            return self._tokens[lane] == token and not self._closed

        def post(callback: Callable[[], None]) -> None:
            if current():
                self._invoke(lambda: callback() if current() else None)

        def target() -> None:
            try:
                self._ready.wait()
                result = work(current, post)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                post(lambda: fail(message))
                return
            post(lambda: apply(result))

        threading.Thread(target=target, daemon=True).start()

    def cancel(self, lane: str) -> None:
        with self._lock:
            self._tokens[lane] += 1

    def close(self) -> None:
        self._closed = True
