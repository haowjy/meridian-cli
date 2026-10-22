"""Validated private Pi coordination contracts; missing files alone mean empty."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictInt

from meridian.lib.state.json_doc import VersionedPrivateDoc


class BashEvidence(VersionedPrivateDoc):
    bash_id: str
    command: str
    cwd: str
    pid: StrictInt | None = Field(ge=1)
    status: Literal["running", "exited", "killed", "timed_out"]
    is_background: StrictBool
    is_tracked: StrictBool
    exit_code: StrictInt | None
    started_at_ms: float
    ended_at_ms: float | None
    log_path: str
    stdout_log_path: str
    stderr_log_path: str
    log_bytes: StrictInt = Field(ge=0)
    timeout_min: float
    originating_bash_id: str | None
    notification_consumed_at_ms: float | None = None
    execution_error: str = ""
    ping_sent_at_ms: float | None = None


class BashEvidenceFile(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float
    records: dict[str, BashEvidence]
    runtime_error: str = ""


class DeliveryReceipts(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    messages: dict[str, list[str]]


class DeliveryObservations(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    observed_message_ids: list[str]


class DeliveryFault(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    operation: Literal["scan", "admission"] | None
    error: str | None


class ClearedSpawns(VersionedPrivateDoc):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float
    cleared_spawn_ids: list[str]
