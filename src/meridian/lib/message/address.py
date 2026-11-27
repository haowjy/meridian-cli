"""The address grammar of ``meridian message``: which receiver a typed address names.

``@…`` is tested here and everything else goes to ``parse_ref``, the one place that knows the
reference forms. Only the form is checked here; whether a session or a person exists is the
route's question. An address that fits no form is an error and is never guessed to be a
person: a mistyped session ID must not end up in Slack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from meridian.lib.core.refs import SELECTORS, parse_ref
from meridian.lib.machines.caller import is_machine_call
from meridian.lib.state.machine_identity import machine_identity_or_none
from meridian.lib.state.saved_machines import SavedMachine

_MAX_PERSON_ADDRESS_BYTES: Final = 256


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


def parse_address(
    raw: str, *, machine: SavedMachine | None = None
) -> SessionAddress | MachineAddress | PersonAddress:
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

    try:
        ref = parse_ref(raw)
    except ValueError as exc:
        raise AddressError("usage", str(exc)) from exc
    if ref.kind not in ("spawn", "chat", "machine"):
        raise AddressError(
            "usage",
            f"`{raw}` is not an address: use a spawn ID (p123), a chat ID (c123), "
            "`<machine>/<id>` or an @person",
        )
    return SessionAddress(ref.value) if machine is None else MachineAddress(machine, ref.value)


__all__ = [
    "AddressError",
    "MachineAddress",
    "PersonAddress",
    "SessionAddress",
    "local_machine_name",
    "parse_address",
]
