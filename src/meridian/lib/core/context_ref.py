"""Resolved ``--from`` context payloads and their prompt rendering.

The spawn resolver (``ops.spawn.context_ref``) builds these at the request boundary;
launch carries and renders them without resolving anything itself.
"""

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.domain import ACTIVE_SPAWN_STATUSES


class SpawnContextRef(BaseModel):
    """Resolved context payload sourced from one spawn."""

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal["spawn"] = "spawn"
    spawn_id: str
    status: str
    agent: str
    desc: str
    model: str
    harness: str
    report_text: str | None = None
    harness_session_id: str | None = None
    chat_id: str | None = None
    history_id: UUID | None = None
    work_id: str | None = None
    work_goal: str | None = None
    task_cwd: str | None = None


class SessionContextRef(BaseModel):
    """Resolved context payload sourced from one chat/session."""

    model_config = ConfigDict(frozen=True)

    ref_kind: Literal["session"] = "session"
    chat_id: str
    primary_spawn_id: str | None = None
    history_id: UUID | None = None
    status: str
    agent: str
    model: str
    harness: str
    report_text: str | None = None
    harness_session_id: str | None = None
    work_id: str | None = None
    work_goal: str | None = None
    task_cwd: str | None = None


type ContextRef = SpawnContextRef | SessionContextRef


def resolved_context_ref_value(ref: ContextRef) -> str:

    if ref.ref_kind == "session":
        return ref.chat_id
    return ref.spawn_id


def _message_line(target: str | None, status: str) -> list[str]:
    # Queued and finalizing spawns have no live control endpoint to take the message.
    if not target or not target.strip() or status != "running":
        return []
    return [f'- Still running; message it: `meridian message {target} "..."`']


def _work_lines(ref: ContextRef) -> list[str]:
    if not ref.work_id:
        return []
    lines = [f"**Work item:** {ref.work_id}"]
    if ref.work_goal:
        # One line, so a goal can't add headings or close the block's tags.
        lines.append(f"**Work goal:** {' '.join(ref.work_goal.split())}")
    return lines


def _catch_up_steps(chat_id: str | None, work_id: str | None) -> list[str]:
    """Numbered steps, cheapest first; commands only, never transcript text."""

    steps: list[str] = []
    if work_id:
        steps.append(
            f"Recorded state first: `meridian work show {work_id}` names the work "
            "item's directory; read its `AGENTS.md`, plan and status there, and check "
            "open PRs."
        )
    if chat_id:
        steps.append(
            f"The end of the old session: `meridian session log {chat_id} "
            "--tail 20` (the user's last messages and the replies)."
        )
        steps.append(
            "Then search, only for what is still missing: "
            f"`meridian session search <query> --ref {chat_id}`."
        )
    return [f"{n}. {step}" for n, step in enumerate(steps, 1)]


def _render_context_ref(ref: ContextRef) -> str:
    status = ref.status or "unknown"
    agent = ref.agent or "n/a"
    if ref.ref_kind == "session":
        chat_id = ref.chat_id
        spawn_id = ref.primary_spawn_id
        tag = f'<prior-session-context chat="{chat_id}"'
        tag += f' primary_spawn="{spawn_id}">' if spawn_id else ">"
        header = [tag, f"# Session {chat_id}"]
        status_line = f"**Status:** {status} | **Agent:** {agent}"
        if spawn_id:
            status_line = f"**Primary spawn:** {spawn_id} | {status_line}"
        close = "</prior-session-context>"
    else:
        chat_id = (ref.chat_id or "").strip() or None
        spawn_id = ref.spawn_id
        header = [f'<prior-spawn-context spawn="{spawn_id}">', f"# Spawn {spawn_id}"]
        status_line = f"**Status:** {status} | **Agent:** {agent} | **Desc:** {ref.desc or 'n/a'}"
        close = "</prior-spawn-context>"

    lines = [*header, status_line]
    if ref.harness_session_id and ref.harness_session_id.strip():
        lines.append(
            f"**Harness:** {ref.harness or 'n/a'} | **Harness session:** {ref.harness_session_id}"
        )
    lines.extend(_work_lines(ref))
    if ref.task_cwd:
        lines.append(f"**Task directory:** `{ref.task_cwd}`")

    has_report = bool(ref.report_text and ref.report_text.strip())
    if ref.report_text and has_report:
        lines.extend(["", "## Report", ref.report_text.strip()])
        commands: list[str] = []
    else:
        if status not in ACTIVE_SPAWN_STATUSES:
            lines.extend(["", "## Report", "No report available."])
        commands = _catch_up_steps(chat_id, ref.work_id)
    if spawn_id:
        commands.append(f"- Details: `meridian spawn show {spawn_id}`")
    if has_report and chat_id:
        commands.append(f"- Session transcript: `meridian session log {chat_id}`")
    # A session ref reaches its primary spawn through the chat ID.
    message_target = spawn_id if ref.ref_kind == "spawn" else chat_id if spawn_id else None
    commands.extend(_message_line(message_target, status))
    if commands:
        lines.extend(["", "## Catch up", *commands])
    lines.append(close)
    return "\n".join(lines)


def render_context_refs(refs: Sequence[ContextRef]) -> str:

    if not refs:
        return ""
    return "\n\n".join(_render_context_ref(ref) for ref in refs)


def context_ref_values(refs: Sequence[ContextRef]) -> tuple[str, ...]:

    return tuple(resolved_context_ref_value(ref) for ref in refs)


__all__ = [
    "ContextRef",
    "SessionContextRef",
    "SpawnContextRef",
    "context_ref_values",
    "render_context_refs",
    "resolved_context_ref_value",
]
