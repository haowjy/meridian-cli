"""The address grammar of ``meridian message``: which receiver a typed address names.

``@…`` and ``inbox`` are tested here and everything else goes to ``parse_ref``, the one place
that knows the reference forms. Only the form is checked here; whether a session or a person
exists is the route's question. An address that fits no form is an error and is never guessed
to be a person: a mistyped session ID must not end up in Slack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from meridian.lib.core.refs import REMOTE_INBOX_ERROR, SELECTORS, parse_ref
from meridian.lib.machines.caller import is_machine_call
from meridian.lib.state.machine_identity import machine_identity_or_none
from meridian.lib.state.saved_machines import SavedMachine

_MAX_PERSON_ADDRESS_BYTES: Final = 256
_SESSION_KINDS: Final = ("spawn", "chat", "machine")


class AddressError(Exception):
    """The address can't be used; *kind* is one of the message error kinds."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(text)
        self.kind = kind
        self.text = text


@dataclass(frozen=True)
class SessionAddress:
    """A session on this machine, by bare spawn or chat ID."""

    id: str


@dataclass(frozen=True)
class MachineAddress:
    """A session on a saved machine, by bare spawn or chat ID."""

    machine: SavedMachine
    id: str


@dataclass(frozen=True)
class PersonAddress:
    """``@…``: a person or a thread, passed to the people program as typed."""

    address: str


@dataclass(frozen=True)
class InboxAddress:
    """``inbox``: this machine's inbox, where a message waits for a session to take it."""


def local_machine_name() -> str | None:
    """This machine's name, for ``machine`` in the JSON; ``None`` when its identity file
    can't be created."""

    identity = machine_identity_or_none()
    return identity.name if identity is not None else None


def _person_address(address: str) -> PersonAddress:
    if len(address) == 1:
        raise AddressError("usage", "an address that starts with @ needs a name after it")
    if any(char.isspace() or not char.isprintable() for char in address):
        raise AddressError("usage", "an @ address has no whitespace or control characters")
    if len(address.encode("utf-8")) > _MAX_PERSON_ADDRESS_BYTES:
        raise AddressError("usage", f"an @ address is at most {_MAX_PERSON_ADDRESS_BYTES} bytes")
    return PersonAddress(address)


def is_address_form(raw: str) -> bool:
    """True when *raw* has the form of a session or person address, on any machine.

    For an address Meridian stores and prints back as a reply command: nothing is
    resolved, so a machine this one has not saved passes.
    """

    if raw.startswith("@"):
        return len(raw) > 1 and raw not in SELECTORS
    try:
        return parse_ref(raw).kind in _SESSION_KINDS
    except ValueError:
        return False


def parse_address(
    raw: str, *, machine: SavedMachine | None = None
) -> SessionAddress | MachineAddress | PersonAddress | InboxAddress:
    """Classify *raw* (already trimmed). *machine* is the other machine the command names,
    from ``other_machine``: the one place that decides it. ``None`` means this machine."""

    if raw.startswith("@"):
        if raw in SELECTORS:
            raise AddressError(
                "usage",
                f"{raw} names a session by its state, not an address: "
                "send to the session's spawn ID or chat ID",
            )
        if machine is not None:
            raise AddressError("usage", "an @ address does not cross machines: drop --machine")
        if is_machine_call():
            raise AddressError("refused", "an @ address doesn't reach another machine")
        return _person_address(raw)

    if raw == "inbox":
        if machine is not None:
            raise AddressError("usage", REMOTE_INBOX_ERROR)
        if is_machine_call():
            raise AddressError("refused", "the inbox doesn't take a message from another machine")
        return InboxAddress()

    try:
        ref = parse_ref(raw)
    except ValueError as exc:
        raise AddressError("usage", str(exc)) from exc
    if ref.kind not in _SESSION_KINDS:
        raise AddressError(
            "usage",
            f"`{raw}` is not an address: use a spawn ID (p123), a chat ID (c123), "
            "`<machine>/<id>`, an @person or `inbox`",
        )
    return SessionAddress(ref.value) if machine is None else MachineAddress(machine, ref.value)


__all__ = [
    "AddressError",
    "InboxAddress",
    "MachineAddress",
    "PersonAddress",
    "SessionAddress",
    "is_address_form",
    "local_machine_name",
    "parse_address",
]
