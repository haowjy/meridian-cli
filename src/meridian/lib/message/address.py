"""The address grammar of ``meridian message``: which receiver a typed address names.

Forms are tested in the order of the machines design's "References and addresses" table.
Only the form is checked here; whether a session or a person exists is the route's
question. An address that fits no form is an error and is never guessed to be a person:
a mistyped session ID must not end up in Slack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from meridian.lib.core.refs import SELECTORS, is_chat_id, is_spawn_id
from meridian.lib.state.machine_identity import machine_identity_or_none

_MACHINE_NAME_RE: Final = re.compile(r"[a-z][a-z0-9-]{0,62}")
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
class PersonAddress:
    """``@…``: a person or a thread, passed to the people program as typed."""

    address: str


def local_machine_name() -> str | None:
    """This machine's name, for ``machine`` in the JSON; ``None`` when its identity file
    can't be created."""

    identity = machine_identity_or_none()
    return identity.name if identity is not None else None


def resolve_machine(name: str) -> None:
    """Accept a machine part that names this machine, by its name or a former one (``was``).

    Raises ``AddressError`` ``not_found`` ("unknown machine") for any other, until saved
    machines exist.
    """

    identity = machine_identity_or_none()
    if identity is None or name not in (identity.name, *identity.was):
        raise AddressError("not_found", f"unknown machine `{name}`")


def _person_address(address: str) -> PersonAddress:
    if len(address) == 1:
        raise AddressError("usage", "an address that starts with @ needs a name after it")
    if any(char.isspace() or not char.isprintable() for char in address):
        raise AddressError("usage", "an @ address has no whitespace or control characters")
    if len(address.encode("utf-8")) > _MAX_PERSON_ADDRESS_BYTES:
        raise AddressError("usage", f"an @ address is at most {_MAX_PERSON_ADDRESS_BYTES} bytes")
    return PersonAddress(address)


def _is_session_id(value: str) -> bool:
    return is_spawn_id(value) or is_chat_id(value)


def parse_address(raw: str, *, machine: str | None = None) -> SessionAddress | PersonAddress:
    """Classify *raw* (already trimmed); *machine* is the ``--machine`` option."""

    if raw.startswith("@"):
        if raw in SELECTORS:
            raise AddressError(
                "usage",
                f"{raw} names a session by its state, not an address: "
                "send to the session's spawn ID or chat ID",
            )
        if machine is not None:
            raise AddressError("usage", "an @ address does not cross machines: drop --machine")
        return _person_address(raw)

    head, slash, tail = raw.partition("/")
    if raw == "inbox" or (slash and tail == "inbox"):
        raise AddressError("usage", "the inbox address is not built yet")

    if slash and _MACHINE_NAME_RE.fullmatch(head) and not _is_session_id(head):
        if not _is_session_id(tail):
            raise AddressError(
                "usage",
                f"`{raw}`: after the machine name, a spawn ID or chat ID is expected "
                "(`<machine>/p123` or `<machine>/c123`)",
            )
        if machine is not None and machine != head:
            raise AddressError("usage", "two machines in one command: use one")
        resolve_machine(head)
        return SessionAddress(tail)

    if _is_session_id(raw):
        if machine is not None:
            resolve_machine(machine)
        return SessionAddress(raw)

    raise AddressError(
        "usage",
        f"`{raw}` is not an address: use a spawn ID (p123), a chat ID (c123), "
        "`<machine>/<id>` or an @person",
    )


__all__ = [
    "AddressError",
    "PersonAddress",
    "SessionAddress",
    "local_machine_name",
    "parse_address",
    "resolve_machine",
]
