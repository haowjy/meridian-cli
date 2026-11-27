"""``meridian message inbox``: build the message from who is calling and file it.

``from``, the project and the default reply address are Meridian's, never a flag's; what
a caller does supply is checked here, so a message can't carry a command to a reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from meridian.lib.inbox.caller import InboxCaller, resolve_caller
from meridian.lib.inbox.fields import (
    MAX_TEXT_BYTES,
    SAFE_FIELD_RULE,
    is_project_id,
    is_reply_address,
    is_safe_field,
)
from meridian.lib.inbox.models import Message, Via
from meridian.lib.inbox.store import InboxError, file_message
from meridian.lib.message.address import local_machine_name
from meridian.lib.message.result import Failed, Filed, MessageResult, Receiver


@dataclass(frozen=True)
class FilingOptions:
    """The flags of ``meridian message`` that belong to the ``inbox`` address."""

    project_id: str | None = None
    reply_to: str | None = None
    via_program: str | None = None
    via_ref: str | None = None
    via_sender: str | None = None

    def given(self) -> list[str]:
        """The flags that were passed, as typed."""

        names = {
            "--project-id": self.project_id,
            "--reply-to": self.reply_to,
            "--via-program": self.via_program,
            "--via-ref": self.via_ref,
            "--via-sender": self.via_sender,
        }
        return [name for name, value in names.items() if value is not None]


def via_from_flags(program: str | None, ref: str | None, sender: str | None) -> Via | None:
    """The claim an outside program makes, from the ``--via-…`` flags."""

    given = {"--via-program": program, "--via-ref": ref, "--via-sender": sender}
    for name, value in given.items():
        if value is not None and not is_safe_field(value):
            raise InboxError(f"{name} takes {SAFE_FIELD_RULE}", kind="usage")
    if program is None:
        if ref is not None or sender is not None:
            raise InboxError(
                "--via-ref and --via-sender need --via-program: the program that says so",
                kind="usage",
            )
        return None
    return Via(program=program, ref=ref, sender=sender)


def _project(caller: InboxCaller, flag: str | None) -> str | None:
    if not caller.is_terminal:
        if flag is not None:
            raise InboxError(
                "--project-id is for a process no session runs; a session's message carries "
                "its own project",
                kind="refused",
            )
        if caller.project is not None and not is_project_id(caller.project):
            # Never filed without a project instead: every session would see it.
            raise InboxError(
                f"this session's project ID {caller.project!r} can't be shown in the inbox: "
                f"a project ID there is {SAFE_FIELD_RULE}. Nothing was filed.",
                kind="refused",
            )
        return caller.project
    # A process with no session has no project of its own: its working directory may be
    # one Meridian chose (a hook row runs in home), and never names one.
    if flag is None:
        return None
    if not is_project_id(flag):
        raise InboxError(
            f"--project-id takes a committed project ID, {SAFE_FIELD_RULE}", kind="usage"
        )
    return flag


def _build(caller: InboxCaller, text: str, options: FilingOptions) -> Message:
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise InboxError(
            f"message is over the {MAX_TEXT_BYTES // 1024} KiB limit: put the content in a "
            "file or the work directory and send its path",
            kind="too_large",
        )
    via = via_from_flags(options.via_program, options.via_ref, options.via_sender)
    project = _project(caller, options.project_id)
    machine = local_machine_name()
    reply_to = options.reply_to
    if reply_to is not None and not is_reply_address(reply_to):
        raise InboxError(
            "--reply-to takes an address `meridian message` accepts (a session ID, "
            f"`<machine>/<id>` or an @person), {SAFE_FIELD_RULE}",
            kind="usage",
        )
    if reply_to is None and caller.chat_id is not None:
        reply_to = f"{machine}/{caller.chat_id}" if machine is not None else caller.chat_id
    sender = caller.party(machine)
    if not caller.is_terminal:
        sender = sender.model_copy(update={"project": project, "agent": caller.agent})
    return Message(
        received=datetime.now(UTC).replace(microsecond=0),
        sender=sender,
        project=project,
        reply_to=reply_to,
        via=via,
        text=text,
    )


def file_from_command(
    text: str,
    options: FilingOptions,
    receiver: Receiver,
    *,
    sender_spawn_id: str | None,
    sender_project_root: Path | None,
) -> MessageResult:
    """File *text* for the caller the invocation names. Every kind but ``maybe_delivered``
    (``MaybeFiled``) means nothing was written. A ``PermissionError`` is the caller's to
    read: only it knows whether a sandbox raised it."""

    try:
        caller = resolve_caller(sender_spawn_id, sender_project_root)
        message_id, _ = file_message(_build(caller, text, options))
    except InboxError as exc:
        return Failed(exc.kind, str(exc), receiver)
    return Filed(message_id, receiver)


__all__ = ["FilingOptions", "file_from_command", "via_from_flags"]
