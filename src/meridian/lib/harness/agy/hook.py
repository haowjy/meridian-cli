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

from meridian.lib.harness.agy import guidance, inbox
from meridian.lib.harness.agy.context import read_segment, scan_since
from meridian.lib.harness.agy.state import (
    agy_root,
    briefed_path,
    is_safe_id,
    process_dir,
    read_json,
    read_seen_offset,
    request_path,
    row_path,
    write_private_json,
    write_seen,
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
    registered = request is None
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

    transcript = payload.get("transcriptPath")
    transcript = transcript if isinstance(transcript, str) else None
    try:
        texts = _guidance(
            proc_dir,
            conversation,
            Path(str(payload.get("cwd") or Path.cwd())),
            transcript,
            registered=registered,
        )
    except Exception:
        # Guidance is best effort: a failure here must not delay the inbox below.
        _log_error("guidance")
        texts = []
    texts.extend(entry.render() for entry in inbox.claim_batch(conversation, transcript))
    return inject_reply(texts) if texts else None


def _guidance(
    proc_dir: Path,
    conversation: str,
    cwd: Path,
    transcript: str | None,
    *,
    registered: bool,
) -> list[str]:
    """The guidance blocks the model's current context lacks.

    Checked when this process registers the conversation (a resumed one may already hold
    them), when the server's row first exists (its facts), and when the context may have
    changed: a compaction, a transcript shorter than the last offset, or no ``seen`` file
    for a registered primary (an older build registered it). ``guard.sh`` starts the hook
    for the row and the first two. The row file is the server's signal that the row exists,
    so nothing waits for it. Markers are written before the text is built: a failure is
    logged by the caller and not retried, as a hook that failed on every call would delay
    the inbox behind it. Whenever this runs, ``seen`` advances to what it read, so
    ``guard.sh`` never rescans it.
    """

    seen = read_seen_offset(proc_dir, conversation)
    scan = scan_since(transcript, seen) if seen is not None else None
    recheck = scan is None or scan.recheck
    row = read_json(row_path(proc_dir, conversation))
    briefed = briefed_path(proc_dir, conversation)
    facts_due = row is not None and not briefed.exists()
    if not (registered or recheck or facts_due):
        if scan is not None and scan.end != seen:
            write_seen(proc_dir, conversation, scan.end, transcript)
        return []

    segment = read_segment(transcript)
    write_seen(proc_dir, conversation, segment.end, transcript)
    if facts_due:
        briefed.parent.mkdir(parents=True, exist_ok=True)
        briefed.touch()

    texts: list[str] = []
    # Capabilities differ across a project's first calls (its state ID appears with its
    # first row), so the static block is matched by its heading, the facts by their text.
    if (registered or recheck) and not segment.holds(guidance.STATIC_HEADING):
        try:
            texts.append(guidance.static_guidance(cwd))
        except Exception:
            _log_error("static guidance")
    if row is not None and (facts_due or recheck):
        try:
            facts = guidance.session_facts(row)
            if not segment.holds(facts):
                texts.append(facts)
        except Exception:
            _log_error("PreInvocation facts")
    return texts


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


__all__ = ["inject_reply", "run_hook", "system_message_step"]
