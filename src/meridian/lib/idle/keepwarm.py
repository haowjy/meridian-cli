"""Keepwarm for harnesses whose sensor takes prompts (``PromptSink``): the stretch's record and
the reply rule. Claude's mod keeps its own copy of both in TypeScript
(``claude_runtime/meridian-idle/hooks/keepwarm.ts``); change them together."""

from __future__ import annotations

from dataclasses import dataclass

STOP = "stop-waiting"
# Every check before a ping stops this long before the cache would expire.
EXPIRY_MARGIN_MS = 60_000

_QUOTES = (("'", "'"), ('"', '"'), ("\u2018", "\u2019"), ("\u201c", "\u201d"), ("`", "`"))


def reply_stops(reply: str) -> bool:
    """Whether the reply's last non-empty line is ``stop-waiting``, alone or in one pair of
    quotes or backticks (``stop-waiting.`` is not)."""

    lines = reply.strip().splitlines()
    line = lines[-1].strip() if lines else ""
    for opening, closing in _QUOTES:
        if len(line) >= 2 and line.startswith(opening) and line.endswith(closing):
            line = line[len(opening) : len(line) - len(closing)]
            break
    return line == STOP


@dataclass
class Stretch:
    """What the sidecar remembers of one stretch's keepwarm. In memory only: a sidecar that
    restarts has none, which sends no more pings (never an extra one)."""

    stretch: int
    # Pings this stretch may take, the reminder's slot included; 0 when keepwarm is off.
    limit: int
    # The gap core placed between a turn's end and the warning, from the arm that placed it.
    lead_ms: int | None = None
    sent: int = 0
    warned: bool = False
    ended: bool = False

    def due(self) -> bool:
        return (
            self.warned and not self.ended and self.sent < self.limit and self.lead_ms is not None
        )
