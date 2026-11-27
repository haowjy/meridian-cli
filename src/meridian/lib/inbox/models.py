from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class HookLogRow(BaseModel):
    name: str
    outcome: str
    exit_code: int | None
    stderr_tail: str


class HookLog(BaseModel):
    fired_at: datetime
    rows: list[HookLogRow]
    errors: list[str]
