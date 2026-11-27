"""The people route: an ``@`` address goes to the program named in ``[message] people``.

Meridian knows nothing of Slack; it runs one program with the address as its last
argument and the text on stdin, and reads one line of JSON back. Who may post stays that
program's decision, made from the environment this module passes on unchanged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from meridian.lib.config.user_only import read_user_only_section, user_config_path
from meridian.lib.message.result import (
    Delivered,
    Failed,
    MessageResult,
    Receiver,
    delivery_kind,
    error_kind,
)
from meridian.lib.utils.json_shapes import is_json_object, is_object_sequence, json_object

PEOPLE_TIMEOUT_SECONDS: Final = 120.0


def people_command(address: str) -> tuple[str, ...] | Failed:
    """The configured command words, or the failure that says how to set them."""

    unset = Failed(
        "no_route",
        f"no program is set up for people (`{address}`); set `[message] people` in "
        f"`{user_config_path()}`",
    )
    try:
        section = read_user_only_section("message")
    except ValueError as error:
        return Failed("no_route", str(error))
    words = section.get("people") if is_json_object(section) else None
    if words is None:
        return unset
    given = words if is_object_sequence(words) else ()
    command = tuple(word for word in given if isinstance(word, str) and word.strip())
    if not command or len(command) != len(given):
        return Failed(
            "no_route",
            f"`[message] people` in `{user_config_path()}` must be a list of words, "
            'for example `["meridian-comms", "deliver"]`',
        )
    return command


def _parse_answer(stdout: bytes, receiver: Receiver, address: str) -> MessageResult:
    """Read the program's one line; anything but a whole answer may have been posted."""

    unanswered = Failed(
        "maybe_delivered",
        f"the people program did not answer properly; the message to {address} may have "
        "been posted and was not resent",
        receiver,
    )
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    first = next((line for line in lines if line.strip()), None)
    if first is None:
        return unanswered
    answer = json_object(json.loads(first))
    if answer is None:
        return unanswered
    ok = answer.get("ok")
    if ok is True:
        note = answer.get("note")
        return Delivered(
            delivery_kind(answer.get("delivery")),
            address,
            note if isinstance(note, str) else None,
            receiver,
        )
    error = json_object(answer.get("error"))
    if ok is False and error is not None and isinstance(error.get("kind"), str):
        text = error.get("text")
        return Failed(
            error_kind(error["kind"], unknown="failed"),
            text.strip() if isinstance(text, str) and text.strip() else f"{address}: not sent",
            receiver,
        )
    return unanswered


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Stop the process this module started, by its own handle: nothing else is touched."""

    if process.returncode is None:
        process.kill()
    await process.wait()


async def deliver_to_person(
    address: str,
    text: str,
    receiver: Receiver,
    *,
    env: Mapping[str, str],
    cwd: Path | None,
) -> MessageResult:
    """Run the people program with *env* and *cwd*; both are what ``meridian`` started with."""

    command = people_command(address)
    if isinstance(command, Failed):
        return Failed(command.kind, command.text, receiver)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            address,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            env=dict(env),
            cwd=cwd,
        )
    except OSError as error:
        return Failed(
            "no_route",
            f"can't run the people program `{command[0]}`: {error.strerror or error}. "
            "Nothing was sent.",
            receiver,
        )
    # One try from the hand-over to a parsed answer: whatever goes wrong in it, the program
    # had the text, so a post may have gone out. A failure nobody listed must not read as
    # `failed`, which tells a script to send again.
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(text.encode("utf-8")), PEOPLE_TIMEOUT_SECONDS
        )
        return _parse_answer(stdout or b"", receiver, address)
    except TimeoutError:
        await _stop(process)
        return Failed(
            "maybe_delivered",
            f"the people program gave no answer in {PEOPLE_TIMEOUT_SECONDS:g} s and was "
            f"stopped; the message to {address} may have been posted and was not resent",
            receiver,
        )
    except Exception as error:
        await _stop(process)
        return Failed(
            "maybe_delivered",
            f"the people program's answer could not be read ({error}); the message to "
            f"{address} may have been posted and was not resent",
            receiver,
        )
    except BaseException:
        await _stop(process)
        raise


__all__ = ["PEOPLE_TIMEOUT_SECONDS", "deliver_to_person", "people_command"]
