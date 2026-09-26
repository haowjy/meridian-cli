"""Bounded legacy candidates. No ambient harness config or cross-project search."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from meridian.lib.core.native_identity import NativeEntryMismatch, NativeSessionUnavailable
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.codex_rollout import CODEX_ROLLOUT_FILENAME_RE, resolve_exact_rollout
from meridian.lib.harness.pi_identity import read_header as read_pi_header
from meridian.lib.harness.pi_identity import resolve_session_file as resolve_pi_session_file
from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.platform import get_home_path
from meridian.lib.state.session_store import SessionRecord
from meridian.lib.state.spawn.model import SpawnRecord

SUPPORTED = frozenset({"claude", "codex", "opencode", "pi"})


class LegacyNativeStores:
    """One import's Codex inventory; other harness candidates are direct paths."""

    def __init__(self) -> None:
        self._codex: dict[Path, dict[str, list[Path]]] = {}

    def candidates(
        self,
        chat: SessionRecord,
        spawns: list[SpawnRecord],
        recorded_cwds: set[Path],
    ) -> set[Path]:
        if chat.native_store:
            return {Path(chat.native_store)}
        adapter = get_default_harness_registry().get(HarnessId(chat.harness))
        facts = [chat, *spawns]
        cwds = {
            Path(value)
            for fact in facts
            for value in (fact.execution_cwd, fact.task_cwd, fact.control_root)
            if value
        }
        cwds.update(recorded_cwds)
        # Snapshots contain explicitly recorded env, not today's shell configuration.
        envs = [
            spawn.launch_policy_snapshot.env
            for spawn in spawns
            if spawn.launch_policy_snapshot and spawn.launch_policy_snapshot.env
        ]
        if chat.harness == "claude":
            env = {"HOME": str(get_home_path())}
            if chat.claude_config_dir:
                env["CLAUDE_CONFIG_DIR"] = chat.claude_config_dir
            return {
                Path(
                    adapter.native_store_for_launch(
                        child_env=env,
                        child_cwd=cwd,
                        spawn_id=SpawnId("legacy"),
                        operation="resume",
                        interactive=True,
                    )
                )
                for cwd in cwds
            }
        if chat.harness == "pi":
            root = resolve_pi_spawn_session_root().resolve()
            return {root, *(root / spawn.id for spawn in spawns)}
        stores: set[Path] = set()
        for env in envs or [{}]:
            recorded_env = {"HOME": str(get_home_path()), **env}
            # Relative recorded config is relative to a recorded launch cwd only.
            for cwd in cwds or {get_home_path()}:
                stores.add(
                    Path(
                        adapter.native_store_for_launch(
                            child_env=recorded_env.copy(),
                            child_cwd=cwd,
                            spawn_id=SpawnId("legacy"),
                            operation="resume",
                            interactive=True,
                        )
                    )
                )
        return stores

    def matching_stores(
        self,
        chat: SessionRecord,
        spawns: list[SpawnRecord],
        session_id: str,
        recorded_cwds: set[Path],
    ) -> tuple[set[Path], bool]:
        matches: set[Path] = set()
        ambiguous = False
        adapter = get_default_harness_registry().get(HarnessId(chat.harness))
        try:
            candidates = self.candidates(chat, spawns, recorded_cwds)
        except NativeSessionUnavailable:
            return matches, ambiguous
        for store in candidates:
            try:
                if chat.harness == "codex":
                    if store not in self._codex:
                        index: dict[str, list[Path]] = defaultdict(list)
                        for path in store.rglob("rollout-*.jsonl"):
                            match = CODEX_ROLLOUT_FILENAME_RE.match(path.name)
                            if match:
                                index[match["session_id"]].append(path)
                        self._codex[store] = index
                    source = resolve_exact_rollout(
                        session_id, self._codex[store].get(session_id, [])
                    )
                else:
                    source = adapter.resolve_native_session_file(
                        session_id=session_id,
                        native_store=store,
                    )
                if source is not None:
                    matches.add(store)
            except NativeEntryMismatch:
                continue
            except NativeSessionUnavailable as exc:
                ambiguous |= exc.reason == "ambiguous_native_file"
        return matches, ambiguous


# --- Evidence about one native session file -------------------------------
#
# Legacy recovery and `session repair` read what a native file says about
# itself: its exact identity (validated by the adapter's own resolver) plus
# cwd, start time and the text used as content proof. Harness file formats
# stay here; the ops layer decides what counts as proof.

PI_SESSION_VERSIONS = frozenset({1, 2, 3})
_PI_FILENAME_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})(?:-(\d{3}))?Z_")


@dataclass(frozen=True)
class NativeSessionEvidence:
    """One validated native session: its exact key plus what it records."""

    harness: str
    path: Path
    native_store: Path
    session_id: str
    cwd: str | None
    started_at: datetime | None
    first_user: str | None = None
    final_assistant: str | None = None


class InvalidNativeSession(ValueError):
    """The file is not a valid, exactly resolvable native session."""


def parse_instant(value: object) -> datetime | None:
    """Aware UTC datetime from an ISO string or epoch milliseconds; else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        with suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _message_text(content: object) -> str | None:
    if isinstance(content, str):
        return content if content.strip() else None
    if not isinstance(content, list):
        return None
    parts = [
        cast("str", part["text"])
        for part in cast("list[object]", content)
        if isinstance(part, dict)
        and cast("dict[str, object]", part).get("type") in {"text", "input_text", "output_text"}
        and isinstance(cast("dict[str, object]", part).get("text"), str)
    ]
    text = "\n".join(parts)
    return text if text.strip() else None


def _jsonl_rows(path: Path) -> Iterator[dict[str, object]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row: object = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield cast("dict[str, object]", row)


def pi_shared_session_root() -> Path:
    """The shared root that primaries used; never evidence for one chat."""
    return resolve_pi_spawn_session_root().resolve()


def pi_spawn_session_dir(spawn_id: str) -> Path:
    """The per-spawn directory 0.6.7 passed to headless Pi runs."""
    return pi_shared_session_root() / spawn_id


def pi_recorded_session_dir(runtime_meta: str | bytes | None) -> Path | None:
    """`session_dir` from a retained `pi_runtime_meta.json`, if recorded."""
    if not runtime_meta:
        return None
    try:
        payload: object = json.loads(runtime_meta)
    except ValueError:
        return None
    value = (
        cast("dict[str, object]", payload).get("session_dir") if isinstance(payload, dict) else None
    )
    return Path(value).expanduser().resolve() if isinstance(value, str) and value.strip() else None


def pi_session_evidence(path: Path, *, final_assistant: bool = False) -> NativeSessionEvidence:
    """Validate a Pi journal (header v1 to v3, exact filename resolution) and read its evidence.

    Reads until the first user message, or to the end when ``final_assistant``.
    """
    path = path.absolute()
    header = read_pi_header(path)  # raises NativeSessionUnavailable
    version = header.get("version", 1)
    if type(version) is not int or version not in PI_SESSION_VERSIONS:
        raise InvalidNativeSession(f"unsupported Pi session version {version!r}: {path}")
    session_id = cast("str", header["id"])
    try:
        resolved = resolve_pi_session_file(path.parent, session_id)
    except (NativeSessionUnavailable, NativeEntryMismatch) as exc:
        raise InvalidNativeSession(f"{path} does not resolve as Pi session {session_id}") from exc
    if resolved != path:
        raise InvalidNativeSession(f"{path} does not resolve as Pi session {session_id}")
    cwd = header.get("cwd")
    started = parse_instant(header.get("timestamp"))
    if started is None:
        match = _PI_FILENAME_TIME.match(path.name)
        if match:
            day, hour, minute, second, millis = match.groups()
            started = parse_instant(f"{day}T{hour}:{minute}:{second}.{millis or '000'}Z")
    first_user: str | None = None
    last_assistant: str | None = None
    for row in _jsonl_rows(path):
        message = row.get("message")
        if row.get("type") != "message" or not isinstance(message, dict):
            continue
        payload = cast("dict[str, object]", message)
        role = payload.get("role")
        if role == "user" and first_user is None:
            first_user = _message_text(payload.get("content"))
            if first_user is not None and not final_assistant:
                break
        elif role == "assistant" and final_assistant:
            last_assistant = _message_text(payload.get("content")) or last_assistant
    return NativeSessionEvidence(
        harness="pi",
        path=path,
        native_store=path.parent,
        session_id=session_id,
        cwd=cwd if isinstance(cwd, str) else None,
        started_at=started,
        first_user=first_user,
        final_assistant=last_assistant,
    )


def pi_sessions_in(
    directory: Path, *, final_assistant: bool = False
) -> list[NativeSessionEvidence]:
    """Valid Pi sessions directly in ``directory``; invalid files are skipped."""
    try:
        paths = sorted(path for path in directory.iterdir() if path.name.endswith(".jsonl"))
    except OSError:
        return []
    found: list[NativeSessionEvidence] = []
    for path in paths:
        with suppress(NativeSessionUnavailable, InvalidNativeSession, OSError):
            found.append(pi_session_evidence(path, final_assistant=final_assistant))
    return found


def claude_session_evidence(path: Path) -> NativeSessionEvidence:
    """Validate a Claude project journal through the adapter's exact resolver."""
    path = path.absolute()
    session_id = path.stem
    adapter = get_default_harness_registry().get(HarnessId("claude"))
    try:
        resolved = adapter.resolve_native_session_file(
            session_id=session_id, native_store=path.parent
        )
    except (NativeSessionUnavailable, NativeEntryMismatch) as exc:
        raise InvalidNativeSession(f"{path} is not Claude session {session_id}") from exc
    if resolved is None or resolved.absolute() != path:
        raise InvalidNativeSession(f"{path} is not Claude session {session_id}")
    cwd: str | None = None
    started: datetime | None = None
    first_user: str | None = None
    for row in _jsonl_rows(path):
        if cwd is None and isinstance(row.get("cwd"), str):
            cwd = cast("str", row["cwd"])
        started = started or parse_instant(row.get("timestamp"))
        message = row.get("message")
        if row.get("type") == "user" and isinstance(message, dict) and first_user is None:
            first_user = _message_text(cast("dict[str, object]", message).get("content"))
        if cwd is not None and started is not None and first_user is not None:
            break
    return NativeSessionEvidence(
        "claude", path, path.parent, session_id, cwd, started, first_user=first_user
    )


def claude_sessions_in(directory: Path) -> list[NativeSessionEvidence]:
    try:
        paths = sorted(path for path in directory.iterdir() if path.suffix == ".jsonl")
    except OSError:
        return []
    found: list[NativeSessionEvidence] = []
    for path in paths:
        with suppress(InvalidNativeSession, OSError):
            found.append(claude_session_evidence(path))
    return found


def codex_session_evidence(path: Path) -> NativeSessionEvidence:
    """A rollout under ``<CODEX_HOME>/sessions/YYYY/MM/DD/``, resolved exactly."""
    path = path.absolute()
    match = CODEX_ROLLOUT_FILENAME_RE.match(path.name)
    if match is None or len(path.parents) < 4:
        raise InvalidNativeSession(f"{path} is not a Codex rollout file")
    session_id = match["session_id"]
    store = path.parents[3]
    try:
        resolved = resolve_exact_rollout(session_id, [path])
    except (NativeSessionUnavailable, NativeEntryMismatch) as exc:
        raise InvalidNativeSession(f"{path} is not Codex session {session_id}") from exc
    if resolved is None:
        raise InvalidNativeSession(f"{path} is not Codex session {session_id}")
    adapter = get_default_harness_registry().get(HarnessId("codex"))
    try:
        exact = adapter.resolve_native_session_file(session_id=session_id, native_store=store)
    except (NativeSessionUnavailable, NativeEntryMismatch) as exc:
        raise InvalidNativeSession(
            f"{path} is not the only Codex rollout for {session_id}"
        ) from exc
    if exact is None or exact.absolute() != path:
        raise InvalidNativeSession(f"{path} is not under a Codex sessions store")
    cwd: str | None = None
    started: datetime | None = None
    first_user: str | None = None
    for row in _jsonl_rows(path):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        body = cast("dict[str, object]", payload)
        if row.get("type") == "session_meta":
            recorded_cwd = body.get("cwd")
            cwd = recorded_cwd if isinstance(recorded_cwd, str) else cwd
            started = parse_instant(body.get("timestamp")) or parse_instant(row.get("timestamp"))
        elif body.get("type") == "message" and body.get("role") == "user":
            first_user = _message_text(body.get("content"))
            if first_user is not None:
                break
    return NativeSessionEvidence("codex", path, store, session_id, cwd, started, first_user)


def opencode_session_evidence(path: Path, session_id: str | None) -> NativeSessionEvidence:
    """OpenCode keeps many sessions in one database: the ID must already be recorded."""
    path = path.absolute()
    if not session_id:
        raise InvalidNativeSession(
            f"{path} holds many OpenCode sessions; this chat records no session ID to select one"
        )
    adapter = get_default_harness_registry().get(HarnessId("opencode"))
    try:
        exact = adapter.resolve_native_session_file(session_id=session_id, native_store=path)
    except (NativeSessionUnavailable, NativeEntryMismatch) as exc:
        raise InvalidNativeSession(f"{path} has no OpenCode session {session_id}") from exc
    if exact is None:
        raise InvalidNativeSession(f"{path} has no OpenCode session {session_id}")
    return NativeSessionEvidence("opencode", path, path, session_id, None, None)


def native_session_evidence(
    harness: str, path: Path, *, recorded_session_id: str | None = None
) -> NativeSessionEvidence:
    """Validate ``path`` as a native session for ``harness`` (manual repair)."""
    if not path.is_file():
        raise InvalidNativeSession(f"{path} is not a file")
    try:
        if harness == "pi":
            return pi_session_evidence(path)
        if harness == "claude":
            return claude_session_evidence(path)
        if harness == "codex":
            return codex_session_evidence(path)
        if harness == "opencode":
            return opencode_session_evidence(path, recorded_session_id)
    except NativeSessionUnavailable as exc:
        raise InvalidNativeSession(f"{path} is not a valid {harness} session") from exc
    raise InvalidNativeSession(f"session repair does not support {harness} chats")
