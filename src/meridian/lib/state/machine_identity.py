"""This machine's identity: ``~/.meridian/machine.json``, written once on first need.

``id`` is what identifies the machine and never changes; ``name`` is how other machines
write it. The default name is computed once and stored, because a hostname can change
by itself (macOS) and a recomputed name would rename the machine under every peer.
"""

import getpass
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.platform.atomic import fsync_directory
from meridian.lib.state.user_paths import get_user_home

FORMAT_VERSION: Final = 1  # the `v` of `_MachineFile`


class MachineIdentityError(RuntimeError):
    """The identity file exists and can't be used, or can't be created."""


@dataclass(frozen=True)
class MachineIdentity:
    id: str
    name: str
    was: tuple[str, ...]


def machine_file() -> Path:
    return get_user_home() / "machine.json"


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


class _MachineFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    v: Literal[1]
    id: str
    name: str = Field(min_length=1)
    was: tuple[str, ...] = ()


def _parse(path: Path) -> MachineIdentity:
    text = path.read_text(encoding="utf-8")
    try:
        data = _MachineFile.model_validate_json(text)
    except ValueError as exc:
        raise MachineIdentityError(
            f"{path} can't be read ({exc}). Fix or remove it; Meridian never rewrites it, "
            "because a new machine ID makes every machine that saved this one refuse it."
        ) from exc
    return MachineIdentity(data.id, data.name, data.was)


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
    "read_or_create_machine_identity",
]
