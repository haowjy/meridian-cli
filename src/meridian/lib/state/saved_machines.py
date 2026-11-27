"""The machines this one can reach: ``~/.meridian/machines.json``, a file only Meridian writes.

The file is read as a JSON document and each entry is checked on its own: a bad entry is
skipped with a warning and the others still work. A write edits the document that was read
and writes that back, so skipped entries and keys a newer build added survive it. A file that
can't be used at all is an error and is never overwritten: that would drop every saved machine.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meridian.lib.core.logging import warn_once
from meridian.lib.core.refs import is_machine_name
from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.utils.json_shapes import is_json_object, json_list

logger = logging.getLogger(__name__)

FORMAT_VERSION: Final = 1
_ID_RE: Final = re.compile(r"[0-9a-f]{12}")
_MAX_TARGET_BYTES: Final = 1024
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")


class SavedMachinesError(RuntimeError):
    """``machines.json`` can't be used (damaged, or written by a newer Meridian)."""


@dataclass(frozen=True)
class SavedMachine:
    name: str
    ssh: str
    id: str
    was: tuple[str, ...] = ()


def machines_file() -> Path:
    return get_user_home() / "machines.json"


def machines_lock_file() -> Path:
    return get_user_home() / "locks" / "machines.lock"


def ssh_target_problem(target: str) -> str | None:
    """Why *target* can't be handed to ``ssh``, or ``None``: empty, starts with ``-`` (``ssh``
    would read an option), a control character, a password, or over 1,024 bytes."""

    if not target.strip():
        return "the SSH target is empty"
    if target.startswith("-"):
        return "the SSH target starts with `-`, which ssh would read as an option"
    if _CONTROL_RE.search(target):
        return "the SSH target holds a control character"
    if len(target.encode("utf-8")) > _MAX_TARGET_BYTES:
        return f"the SSH target is over {_MAX_TARGET_BYTES} bytes"
    userinfo, at, _ = target.removeprefix("ssh://").rpartition("@")
    if at and ":" in userinfo:
        return "the SSH target carries a password: use a key, or a Host in ~/.ssh/config"
    return None


def _entry(raw: object, names: set[str], ids: set[str]) -> SavedMachine | str:
    """The entry, or why it is skipped."""

    if not is_json_object(raw):
        return "not an object"
    name, ssh, ident = raw.get("name"), raw.get("ssh"), raw.get("id")
    if not isinstance(name, str) or not is_machine_name(name):
        return "`name` breaks the machine name rule"
    if not isinstance(ssh, str):
        return "`ssh` is not a string"
    if (problem := ssh_target_problem(ssh)) is not None:
        return f"`ssh` can't be used ({problem})"
    if not isinstance(ident, str) or _ID_RE.fullmatch(ident) is None:
        return "`id` is not 12 hex characters"
    was = json_list(raw["was"]) if "was" in raw else []
    if was is None or not all(isinstance(item, str) and is_machine_name(item) for item in was):
        return "`was` is not a list of machine names"
    if name in names or ident in ids:
        return "its name or ID is already saved by an earlier entry"
    return SavedMachine(name, ssh, ident, tuple(item for item in was if isinstance(item, str)))


def _load() -> tuple[dict[str, object], list[object]]:
    """The document and its ``machines`` list (the same list object, so an edit shows in it)."""

    path = machines_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        machines: list[object] = []
        return {"v": FORMAT_VERSION, "machines": machines}, machines
    except (OSError, UnicodeDecodeError) as exc:
        raise SavedMachinesError(f"{path} can't be read ({exc})") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise SavedMachinesError(f"{path} is not JSON ({exc}). Fix or remove it.") from exc
    if not is_json_object(document):
        raise SavedMachinesError(f"{path} is not a JSON object. Fix or remove it.")
    version = document.get("v")
    if version != FORMAT_VERSION:
        if isinstance(version, int) and version > FORMAT_VERSION:
            raise SavedMachinesError(
                f"{path} was written by a newer Meridian (format {version}); upgrade this one."
            )
        raise SavedMachinesError(f"{path} has no usable `v` (expected {FORMAT_VERSION}).")
    listed = json_list(document.setdefault("machines", []))
    if listed is None:
        raise SavedMachinesError(f"{path}: `machines` is not a list. Fix or remove it.")
    return document, listed


def _parse(machines: list[object]) -> list[SavedMachine]:
    names: set[str] = set()
    ids: set[str] = set()
    found: list[SavedMachine] = []
    for index, raw in enumerate(machines):
        entry = _entry(raw, names, ids)
        if isinstance(entry, str):
            warn_once(
                ("saved-machine-skipped", index, entry),
                lambda index=index, entry=entry: logger.warning(
                    "%s: skipping entry %d: %s", machines_file(), index, entry
                ),
            )
            continue
        names.add(entry.name)
        ids.add(entry.id)
        found.append(entry)
    return found


def read_saved_machines() -> list[SavedMachine]:
    """The usable entries, in file order. A missing file is an empty list; takes no lock,
    because the file is replaced whole."""

    return _parse(_load()[1])


def update_saved_machines[T](edit: Callable[[list[SavedMachine], list[object]], T]) -> T:
    """Apply *edit* to the list as it is under the lock, and write it back when it changed.

    *edit* gets the usable entries and the document's raw ``machines`` list, which it changes
    in place: raw dicts, so skipped entries and unknown keys stay as they were.
    """

    lock = machines_lock_file()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock_file(lock):
        document, machines = _load()
        before = copy.deepcopy(machines)
        result = edit(_parse(machines), machines)
        if machines != before:
            atomic_write_text(
                machines_file(), json.dumps(document, indent=1, sort_keys=True) + "\n"
            )
        return result


def _raw_fields(machines: list[object], machine_id: str) -> dict[str, object] | None:
    for raw in machines:
        if is_json_object(raw) and raw.get("id") == machine_id:
            return raw
    return None


def _drop_from_was(machines: list[object], name: str, *, keep_id: str | None) -> None:
    for raw in machines:
        if not is_json_object(raw) or raw.get("id") == keep_id:
            continue
        was = json_list(raw.get("was"))
        if was is not None and name in was:
            raw["was"] = [item for item in was if item != name]


def add_machine(machines: list[object], entry: SavedMachine) -> None:
    """Append *entry*; its name leaves every ``was``, because a current name beats a former one."""

    _drop_from_was(machines, entry.name, keep_id=None)
    document: dict[str, object] = {"name": entry.name, "ssh": entry.ssh, "id": entry.id}
    if entry.was:
        document["was"] = list(entry.was)
    machines.append(document)


def remove_machine(machines: list[object], name: str) -> bool:
    for raw in machines:
        if is_json_object(raw) and raw.get("name") == name:
            machines.remove(raw)
            return True
    return False


@dataclass(frozen=True)
class NameTaken:
    """The new name is another saved machine's current name, or this machine's own."""

    holder: str


def set_machine_name(
    saved: list[SavedMachine], machines: list[object], machine_id: str, new_name: str, own_name: str
) -> str | NameTaken | None:
    """Inside ``update_saved_machines``: this ID is now called *new_name*.

    The old name goes to the front of the entry's ``was`` and leaves every other ``was``; the
    new name leaves every ``was``. Returns the old name, ``None`` when nothing changed, or
    ``NameTaken`` and changes nothing: two entries with one name would make a later read skip
    one of them by file order.
    """

    current = next((entry for entry in saved if entry.id == machine_id), None)
    fields = _raw_fields(machines, machine_id)
    if current is None or fields is None or current.name == new_name:
        return None
    if new_name == own_name:
        return NameTaken("this machine")
    for other in saved:
        if other.id != machine_id and other.name == new_name:
            return NameTaken(other.name)
    _drop_from_was(machines, new_name, keep_id=None)
    _drop_from_was(machines, current.name, keep_id=machine_id)
    fields["was"] = [current.name, *(name for name in current.was if name != new_name)]
    fields["name"] = new_name
    return current.name


__all__ = [
    "NameTaken",
    "SavedMachine",
    "SavedMachinesError",
    "add_machine",
    "machines_file",
    "machines_lock_file",
    "read_saved_machines",
    "remove_machine",
    "set_machine_name",
    "ssh_target_problem",
    "update_saved_machines",
]
