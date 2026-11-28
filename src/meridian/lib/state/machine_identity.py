"""This machine's identity: ``~/.meridian/machine.json``, written once on first need.

``id`` is what identifies the machine and never changes; ``name`` is how other machines
write it. The default name is computed once and stored, because a hostname can change
by itself (macOS) and a recomputed name would rename the machine under every peer.

The file is read as a JSON document, and a rename edits the document it read and writes
that back, so keys a newer build added survive it. A file with a higher ``v`` is refused
with "upgrade", never "remove": a removed file gives the machine a new ID.
"""

import getpass
import json
import logging
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meridian.lib.core.refs import is_machine_id, is_machine_name
from meridian.lib.platform.atomic import atomic_write_text, fsync_directory
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.utils.json_shapes import is_json_object, json_list

logger = logging.getLogger(__name__)

FORMAT_VERSION: Final = 1


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


def _load(path: Path) -> dict[str, object]:
    """The file as a JSON document of this format. ``FileNotFoundError`` when there is none."""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise MachineIdentityError(f"{path} can't be read ({exc})") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise _damaged(path, f"is not JSON ({exc})") from exc
    if not is_json_object(document):
        raise _damaged(path, "is not a JSON object")
    version = document.get("v")
    if isinstance(version, bool) or not isinstance(version, int) or version < FORMAT_VERSION:
        raise _damaged(path, f"has no usable `v` (expected {FORMAT_VERSION})")
    if version > FORMAT_VERSION:
        raise MachineIdentityError(
            f"{path} was written by a newer Meridian (format {version}); upgrade this one."
        )
    return document


def _damaged(path: Path, why: str) -> MachineIdentityError:
    return MachineIdentityError(
        f"{path} {why}. Fix it by hand: Meridian never rewrites it. Removing it gives this "
        "machine a new ID, and every machine that saved this one then refuses it until it "
        "removes and adds this machine again."
    )


def _identity(path: Path, document: dict[str, object]) -> MachineIdentity:
    ident, name = document.get("id"), document.get("name")
    if not isinstance(ident, str) or not is_machine_id(ident):
        raise _damaged(path, "has an `id` that is not 12 hex characters")
    if not isinstance(name, str) or not is_machine_name(name):
        raise _damaged(path, "has a `name` that breaks the machine name rule")
    was = json_list(document["was"]) if "was" in document else []
    if was is None:
        raise _damaged(path, "has a `was` that is not a list of machine names")
    former = tuple(item for item in was if isinstance(item, str) and is_machine_name(item))
    if len(former) != len(was):
        raise _damaged(path, "has a `was` that is not a list of machine names")
    return MachineIdentity(ident, name, former)


def _parse(path: Path) -> MachineIdentity:
    return _identity(path, _load(path))


def _dump(document: dict[str, object]) -> str:
    return json.dumps(document, separators=(",", ":")) + "\n"


def rename_machine(new_name: str) -> MachineIdentity:
    """Give this machine *new_name*; the old name goes to the front of ``was`` and the new one
    leaves it (renaming back). The file is read again under the lock, and the document that
    was read is written back with the two keys changed.

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
            read_or_create_machine_identity()
            document = _load(path)
            current = _identity(path, document)
            if current.name == new_name:
                return current
            was = (current.name, *(name for name in current.was if name != new_name))
            document["name"] = new_name
            document["was"] = list(was)
            atomic_write_text(path, _dump(document))
    except OSError as exc:
        raise MachineIdentityError(f"can't write {path}: {exc}") from exc
    return MachineIdentity(current.id, new_name, was)


def _create(path: Path) -> None:
    """Write the file with a temp file and ``link(2)``: the loser of a race links nothing."""

    try:
        login = getpass.getuser()
    except (KeyError, OSError):
        login = "user"
    document: dict[str, object] = {
        "v": FORMAT_VERSION,
        "id": secrets.token_hex(6),
        "name": default_machine_name(login, socket.gethostname().split(".")[0]),
        "was": [],
    }
    temp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_dump(document))
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
