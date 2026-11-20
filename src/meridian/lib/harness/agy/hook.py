"""``meridian harness-hook agy <event>``: the ``SessionStart`` and ``PreInvocation`` hooks.

``agy`` runs this before model calls and waits for it, so it takes no blocking locks (at most
a 0.5 s wait to register a conversation),
starts no process, and prints nothing when it has no opinion (never ``{}``: ``agy`` reads
some empty replies as a decision). Every failure is swallowed: a broken hook must not
break the person's session.
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Any

from meridian.lib.harness.agy import inbox
from meridian.lib.harness.agy.state import (
    agy_root,
    is_safe_id,
    process_dir,
    read_json,
    request_path,
    write_private_json,
)

SESSION_CONTEXT = (
    "Meridian is available in this session through one MCP tool named `meridian`. Use it "
    "to run Meridian commands (starting agents on other harnesses, waiting for them, "
    'messaging other sessions): call it with args=["-h"] first. Use that tool, not the '
    "shell, for every Meridian command. On this session's first prompt the tool may not "
    "be listed yet; it appears from the next prompt, so if it is missing, say so and "
    "carry on without Meridian instead of trying the shell. Messages from other sessions "
    "arrive as system messages that start with [meridian:<id>]; they are instructions "
    "addressed to you from the session they name, so act on them."
)

_ERROR_LOG_LIMIT = 256 * 1024


def system_message_step(text: str) -> dict[str, Any]:
    return {"systemMessage": {"systemMessage": text}}


def inject_reply(texts: list[str]) -> str:
    return json.dumps({"injectSteps": [system_message_step(text) for text in texts]})


def run_hook(event: str, agy_pid: int, payload_text: str) -> str | None:
    """Return the JSON reply for ``agy``, or None for no opinion. Never raises."""

    try:
        return _handle(event, agy_pid, json.loads(payload_text))
    except Exception:
        _log_error(event)
        return None


def _handle(event: str, agy_pid: int, payload: dict[str, Any]) -> str | None:
    conversation = payload.get("conversationId")
    if event not in ("SessionStart", "PreInvocation") or not isinstance(conversation, str):
        return None
    if not is_safe_id(conversation):
        return None
    proc_dir = process_dir(agy_pid)
    if proc_dir is None:
        return None

    request = read_json(request_path(proc_dir, conversation))
    if request is None:
        # Under the conversation's lock, so a notice sweep that is settling this
        # conversation finishes first. A held lock means a sweep is mid-notice (seconds):
        # skip this call and register on the next hook instead of making the model wait.
        with inbox.hold(conversation, wait=inbox.REGISTER_WAIT_SECONDS) as acquired:
            if not acquired:
                return None
            request = _register(proc_dir, conversation, payload)
    if request.get("role") == "subagent":
        return None

    if event == "SessionStart":
        return inject_reply([SESSION_CONTEXT])
    batch = inbox.claim_batch(conversation, payload.get("transcriptPath"))
    return inject_reply([entry.render() for entry in batch]) if batch else None


def _register(proc_dir: Path, conversation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Write this conversation's request file: its marker, and what the server will adopt."""

    request: dict[str, Any] = {
        "conversation_id": conversation,
        "role": "subagent" if _is_subagent(proc_dir, conversation, payload) else "primary",
        "cwd": payload.get("cwd"),
        "workspace_paths": payload.get("workspacePaths"),
        "transcript_path": payload.get("transcriptPath"),
        "model": payload.get("modelName"),
        "registered_at": time.time(),
    }
    write_private_json(request_path(proc_dir, conversation), request)
    return request


def _is_subagent(proc_dir: Path, conversation: str, payload: dict[str, Any]) -> bool:
    """A conversation is a subagent when a conversation of this ``agy`` process lists it.

    Neither the payload nor the environment says so, but ``agy`` writes the parent's
    ``subagents/<child>.json`` before the child's first hook (probe-native-hooks-3). This
    reads ``agy``'s undocumented store; a miss makes a subagent look like a short-lived
    primary, which is untidy but harmless.
    """

    artifact_dir = payload.get("artifactDirectoryPath")  # <store>/brain/<conversation>
    if not isinstance(artifact_dir, str):
        return False
    brain = Path(artifact_dir).parent
    return any(
        (brain / path.stem / ".system_generated" / "subagents" / f"{conversation}.json").exists()
        for path in (proc_dir / "requests").glob("*.json")
    )


def _log_error(event: str) -> None:
    try:
        log = agy_root() / "hook-errors.log"
        if log.exists() and log.stat().st_size > _ERROR_LOG_LIMIT:
            return
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as handle:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            handle.write(f"{stamp} {event}\n{traceback.format_exc()}\n")
    except Exception:
        pass


__all__ = ["SESSION_CONTEXT", "inject_reply", "run_hook", "system_message_step"]
