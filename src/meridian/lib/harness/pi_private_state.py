"""Validated private Pi coordination contracts; missing files alone mean empty."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator


class PrivateModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", allow_inf_nan=False, strict=True)

    @field_validator("v", mode="before", check_fields=False)
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("private-file version must be integer 1")
        return value


class BashEvidence(PrivateModel):
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


class BashEvidenceFile(PrivateModel):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float
    records: dict[str, BashEvidence]
    runtime_error: str = ""


class DeliveryReceipts(PrivateModel):
    v: Literal[1]
    spawn_id: str
    messages: dict[str, list[str]]


class DeliveryObservations(PrivateModel):
    v: Literal[1]
    spawn_id: str
    observed_message_ids: list[str]


class DeliveryFault(PrivateModel):
    v: Literal[1]
    spawn_id: str
    operation: Literal["scan", "admission"] | None
    error: str | None


class ClearedSpawns(PrivateModel):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float
    cleared_spawn_ids: list[str]


class WaitReservation(PrivateModel):
    owner_pid: StrictInt = Field(gt=0)
    owner_birth_epoch: float = Field(gt=0)
    expires_at_epoch: float
    spawn_ids: list[str]


class SpawnObservations(PrivateModel):
    v: Literal[1]
    spawn_id: str
    updated_at_ms: float = 0.0
    observed_spawn_ids: list[str]
    waiting_spawn_ids: list[str] = Field(default_factory=list)
    wait_reservations: dict[str, WaitReservation] = Field(default_factory=dict)
