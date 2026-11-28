"""What the inbox's files hold. A file that fails a check here reads as unparseable and is
skipped, so a file written by hand can't put an unchecked field in front of a reader.
That includes a time with no zone: one compared with the clock would raise in the middle
of a listing or a prune pass."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from meridian.lib.core.refs import is_machine_id
from meridian.lib.inbox.fields import is_project_id, is_reply_address, is_safe_field, shown
from meridian.lib.state.machine_identity import MachineIdentity


def _safe(value: str) -> str:
    if not is_safe_field(value):
        raise ValueError("not in the inbox's safe character set")
    return value


def _project(value: str) -> str:
    if not is_project_id(value):
        raise ValueError("not a committed project ID")
    return value


def _machine_id(value: str) -> str:
    if not is_machine_id(value):
        raise ValueError("not a machine ID")
    return value


def _reply_address(value: str) -> str:
    if not is_reply_address(value):
        raise ValueError("not a message address")
    return value


SafeField = Annotated[str, AfterValidator(_safe)]
ProjectId = Annotated[str, AfterValidator(_project)]


class _Doc(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="ignore", populate_by_name=True, serialize_by_alias=True
    )


class Party(_Doc):
    """Who filed or took a message, as Meridian found it: never a caller's flag."""

    kind: Literal["session", "terminal"]
    machine: SafeField | None = None
    """The machine's name when the party was recorded: a label and a reply address. A
    machine can be renamed, so it never decides whose a claim is."""
    machine_id: Annotated[str, AfterValidator(_machine_id)] | None = None
    """The machine. A file written before this was stored has only the name."""
    id: SafeField | None = None
    """A session's chat ID, the ID a reply address uses."""
    project: ProjectId | None = None
    agent: SafeField | None = None
    """The session's profile name, which a repository chooses."""

    def machine_id_from(self, here: MachineIdentity | None) -> str | None:
        """The ID of the party's machine, *here* being this one. A party with only a name
        is this machine's when the name is its current or a former one; ``None`` when the
        name is some other machine's."""

        if self.machine_id is not None or here is None:
            return self.machine_id
        return here.id if self.machine in (here.name, *here.was) else None

    def same_as(self, other: Party, here: MachineIdentity | None) -> bool:
        if (self.kind, self.id) != (other.kind, other.id):
            return False
        mine, theirs = self.machine_id_from(here), other.machine_id_from(here)
        if mine is not None and theirs is not None:
            return mine == theirs
        return self.machine == other.machine

    def label(self) -> str:
        if self.kind == "terminal":
            # An inbox holds messages from other machines too: say whose terminal.
            return f"terminal on {self.machine}" if self.machine else "terminal"
        where = f"{self.machine}/{self.id}" if self.machine else str(self.id)
        return f"{where} ({shown(self.agent)})" if self.agent else where


class Via(_Doc):
    """What an outside program says about a message or a claim. Always shown as a claim."""

    program: SafeField
    ref: SafeField | None = None
    sender: SafeField | None = None

    def label(self) -> str:
        """As it is always shown: the program's claim, never a fact."""

        return f"via {self.program}: {self.sender}" if self.sender else f"via {self.program}"


class Message(_Doc):
    received: AwareDatetime
    sender: Party = Field(validation_alias="from", serialization_alias="from")
    project: ProjectId | None = None
    reply_to: Annotated[str, AfterValidator(_reply_address)] | None = None
    via: Via | None = None
    text: str


class Claim(_Doc):
    by: Party
    via: Via | None = None
    at: AwareDatetime

    def same_taker(self, by: Party, via: Via | None, here: MachineIdentity | None) -> bool:
        mine = (via.program, via.sender) if via is not None else None
        theirs = (self.via.program, self.via.sender) if self.via is not None else None
        return self.by.same_as(by, here) and mine == theirs


class Offer(_Doc):
    to: SafeField
    at: AwareDatetime


class Offers(_Doc):
    offers: tuple[Offer, ...] = ()


class HookLogRow(BaseModel):
    name: str
    outcome: str
    exit_code: int | None
    stdout_tail: str = ""
    """A Meridian command run with ``--json`` says why it failed on stdout."""
    stderr_tail: str


class HookLog(BaseModel):
    """What the ``inbox.received`` rows did for one message."""

    fired_at: datetime
    rows: list[HookLogRow]
    errors: list[str]


__all__ = [
    "Claim",
    "HookLog",
    "HookLogRow",
    "Message",
    "Offer",
    "Offers",
    "Party",
    "Via",
]
