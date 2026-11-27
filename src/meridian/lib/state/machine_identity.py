"""This machine's identity: ``~/.meridian/machine.json``, written once on first need.

``id`` is what identifies the machine and never changes; ``name`` is how other machines
write it. The default name is computed once and stored, because a hostname can change
by itself (macOS) and a recomputed name would rename the machine under every peer.
"""

import getpass
import logging
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from meridian.lib.core.refs import is_machine_name
from meridian.lib.platform.atomic import atomic_write_text, fsync_directory
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.user_paths import get_user_home

logger = logging.getLogger(__name__)

FORMAT_VERSION: Final = 1  # the `v` of `_MachineFile`
_ID_RE: Final = re.compile(r"[0-9a-f]{12}")


class MachineIdentityError(RuntimeError):
    """The identity file exists and can't be used, or can't be created."""


@dataclass(frozen=True)
class MachineIdentity:
    id: str
    name: str
    was: tuple[str, ...]


def machine_file() -> Path:
    return get_user_home() / "machine.json"


def machine_lock_file() -> Path:
    """Guards a rename only: creating the file is ``link(2)``, which needs no lock."""

    return get_user_home() / "locks" / "machine.lock"


def default_machine_name(login: str, host: str) -> str:
    """``<login>-<short host>`` made to fit ``[a-z][a-z0-9-]{0,62}``: lower-case, hyphens for
    anything outside ``a-z0-9-``, a leading ``m`` if it doesn't start with a letter,
    runs of hyphens joined, at most 63 characters, no trailing hyphen."""

    name = re.sub(r"[^a-z0-9-]", "-", f"{login}-{host}".lower())
    if not name[:1].isalpha():
        name = f"m{name}"
    return re.sub(r"-+", "-", name)[:63].rstrip("-")


def read_or_create_machine_identity() -> MachineIdentity:
    """Return this machine's identity, creating the file when it does not exist.

    A file that can't be parsed raises: regenerating it would give the machine a new ID,
    and every peer that saved the old one would refuse it.
    """

    path = machine_file()
    try:
        return _parse(path)
    except FileNotFoundError:
        pass
    _create(path)
    return _parse(path)


def machine_identity_or_none() -> MachineIdentity | None:
    """``read_or_create_machine_identity``, or ``None`` when the file can't be used (a
    read-only home, a damaged file). A caller that only labels output goes on without it."""

    try:
        return read_or_create_machine_identity()
    except MachineIdentityError as exc:
        logger.warning("machine identity unavailable: %s", exc)
        return None


class _MachineFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    v: Literal[1]
    id: str
    name: str
    was: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _id_is_12_hex(cls, value: str) -> str:
        if _ID_RE.fullmatch(value) is None:
            raise ValueError("`id` must be 12 hex characters")
        return value

    @field_validator("name")
    @classmethod
    def _name_fits_rule(cls, value: str) -> str:
        if not is_machine_name(value):
            raise ValueError(f"`name` {value!r} breaks the machine name rule")
        return value

    @field_validator("was")
    @classmethod
    def _was_fit_rule(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not is_machine_name(name):
                raise ValueError(f"`was` entry {name!r} breaks the machine name rule")
        return value


def _parse(path: Path) -> MachineIdentity:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, MachineIdentityError):
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise MachineIdentityError(f"{path} can't be read ({exc})") from exc
    try:
        data = _MachineFile.model_validate_json(text)
    except ValueError as exc:
        raise MachineIdentityError(
            f"{path} can't be read ({exc}). Fix or remove it; Meridian never rewrites it, "
            "because a new machine ID makes every machine that saved this one refuse it."
        ) from exc
    return MachineIdentity(data.id, data.name, data.was)


def rename_machine(new_name: str) -> MachineIdentity:
    """Give this machine *new_name*; the old name goes to the front of ``was`` and the new one
    leaves it (renaming back). The file is read again under the lock, then replaced whole.

    Raises ``MachineIdentityError`` for a name that breaks the rule or a file that can't be used.
    """

    if not is_machine_name(new_name):
        raise MachineIdentityError(
            f"`{new_name}` can't be a machine name: it must match [a-z][a-z0-9-]{{0,62}}, "
            "and not be `inbox` or look like a spawn or chat ID (p123, c123)"
        )
    path = machine_file()
    try:
        with lock_file(machine_lock_file()):
            current = read_or_create_machine_identity()
            if current.name == new_name:
                return current
            was = (current.name, *(name for name in current.was if name != new_name))
            document = _MachineFile(v=FORMAT_VERSION, id=current.id, name=new_name, was=was)
            atomic_write_text(path, document.model_dump_json() + "\n")
    except OSError as exc:
        raise MachineIdentityError(f"can't write {path}: {exc}") from exc
    return MachineIdentity(document.id, document.name, document.was)


def _create(path: Path) -> None:
    """Write the file with a temp file and ``link(2)``: the loser of a race links nothing."""

    try:
        login = getpass.getuser()
    except (KeyError, OSError):
        login = "user"
    document = _MachineFile(
        v=FORMAT_VERSION,
        id=secrets.token_hex(6),
        name=default_machine_name(login, socket.gethostname().split(".")[0]),
    )
    temp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(document.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return
        fsync_directory(path.parent)
    except OSError as exc:
        raise MachineIdentityError(f"can't create {path}: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)


__all__ = [
    "MachineIdentity",
    "MachineIdentityError",
    "default_machine_name",
    "machine_file",
    "machine_identity_or_none",
    "read_or_create_machine_identity",
    "rename_machine",
]
