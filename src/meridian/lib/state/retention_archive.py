"""Immutable portable ZIPs, verified streams, and file-authoritative receipts.

This module owns byte mechanics, not eligibility. Publication never removes
sources; the policy layer must independently revalidate its capture before reclaim.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal, NamedTuple
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.lib.platform.atomic import fsync_directory, is_atomic_temp_name
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import append_durable_jsonl_line, atomic_write_text
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.history_codec import TranscriptHeader
from meridian.lib.state.native_snapshot import (
    NATIVE_SNAPSHOT_FILENAME,
    TranscriptValidation,
    canonical_transcript_member,
    complete_published_snapshot,
    read_snapshot,
)
from meridian.lib.state.session_store import SessionRecord
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import StoredSpawnState, record_to_stored_state

_PREFIX = "meridian-history-v1/"
_MANIFEST = _PREFIX + "manifest.json"
_MAX_MEMBERS = 100_000
_MAX_METADATA = 32 * 1024 * 1024
_MAX_CONTENT = 1024**4  # ZIP64, including a single oversized record.
_EXCLUDED = frozenset(
    {
        "heartbeat",
        "process_scopes.json",
        "reaper_cleanup_claim.json",
        "finalize-evidence.json",
        "restored-from.json",
    }
)


class Member(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    size: int = Field(ge=0, le=_MAX_CONTENT)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArchivedRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    history_id: UUID
    state: SpawnRecord
    session: SessionRecord | None = None
    activity: str
    files: tuple[Member, ...]
    required_files: tuple[str, ...]
    portable_digest: str

    @model_validator(mode="before")
    @classmethod
    def _discard_replaced_capture_fingerprint(cls, value: Any) -> Any:
        # Published ZIPs must remain readable after removing this derived field.
        # It was never portable authority; new records no longer compute/store it.
        if isinstance(value, dict) and "capture_fingerprint" in value:
            return {key: item for key, item in value.items() if key != "capture_fingerprint"}
        return value


class ArchiveManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    archive_id: UUID
    created_at: str
    records: tuple[ArchivedRecord, ...]
    members: tuple[Member, ...]


class ArchiveReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event: Literal["published", "reclaim_prepared", "reclaimed", "imported"]
    archive_id: UUID
    location_id: UUID
    destination: str
    zip_name: str
    manifest_sha256: str
    records: tuple[ArchivedRecord, ...]


class ArchiveValidationError(ValueError):
    """Archive structure cannot establish a complete, verified record."""


class SourceWitness(NamedTuple):
    files: tuple[tuple[object, ...], ...]
    state: SpawnRecord | None
    session: SessionRecord | None


def source_witness(directory: Path) -> SourceWitness:
    """Detect changes after hashing; never substitute metadata for checksums."""
    from meridian.lib.state.session_identity import session_records_for_spawns
    from meridian.lib.state.spawn.repository import read_state

    paths = (directory, *sorted(directory.rglob("*")))
    files = tuple(
        (
            str(path.relative_to(directory)),
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        for path in paths
        for info in (path.lstat(),)
    )
    current = read_state(directory.parent, directory.name, include_prompt=False)
    session = (
        session_records_for_spawns(directory.parent.parent, (current,)).get(current.id)
        if current is not None
        else None
    )
    return SourceWitness(files, current, session)


@contextmanager
def verified_source(directory: Path) -> Generator[SourceWitness]:
    """Hold source authority stable during verification without blocking other writers."""
    root = directory.parent.parent
    source = HistorySource(kind="spawn", key=directory.name)
    with (
        lock_file(HistoryChanges(root).mutation_lock, mode="shared"),
        lock_file(source.lock_path(root)),
    ):
        witness = source_witness(directory)
        yield witness
        if source_witness(directory) != witness:
            raise ValueError("Source changed during verification; retry")


class ArchiveUnavailable(FileNotFoundError):
    """None of the selected equivalent locations can be read safely."""


class ArchiveLocation(NamedTuple):
    receipt: ArchiveReceipt
    path: Path


class _LocationMarker(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    location_id: UUID


ARCHIVE_READ_ERRORS = (ValueError, OSError, EOFError, zipfile.BadZipFile, zlib.error)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def safe_member_name(name: str) -> str:
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or ":" in name
        or name.startswith("/")
        or unicodedata.normalize("NFC", name) != name
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise ValueError(f"Unsafe archive member: {name!r}")
    if PurePosixPath(name).as_posix() != name:
        raise ValueError(f"Noncanonical archive member: {name!r}")
    return name


def _is_reserved_atomic_temp(name: str) -> bool:
    from meridian.lib.launch.constants import HISTORY_FILENAME

    return any(
        is_atomic_temp_name(name, reserved)
        for reserved in (NATIVE_SNAPSHOT_FILENAME, HISTORY_FILENAME)
    )


def inventory(directory: Path) -> tuple[Member, ...]:
    if directory.is_symlink():
        raise ValueError("Retained record directory must not be a symlink")
    members: list[Member] = []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        safe_member_name(relative)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Symlink in retained record: {path}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"Non-regular or hard-linked retained file: {path}")
        if (
            path.name in _EXCLUDED
            or path.suffix in {".lock", ".sock", ".sentinel"}
            or _is_reserved_atomic_temp(path.name)
        ):
            continue
        checksum = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                checksum.update(chunk)
                size += len(chunk)
        members.append(Member(name=relative, size=size, sha256=checksum.hexdigest()))
    names = {member.name for member in members}
    transcript = canonical_transcript_member(names)
    required = {"state.json", transcript}
    if "state.json" not in names:
        raise ValueError("A retained record requires state.json")
    stored = StoredSpawnState.model_validate_json((directory / "state.json").read_bytes())
    if stored.prompt_length is not None:
        required.add("starting-prompt.md")
    if not required <= names:
        missing = sorted(required - names)
        raise ValueError(f"Missing required retained record member: {missing}")
    if transcript == NATIVE_SNAPSHOT_FILENAME:
        validation = complete_published_snapshot(
            directory / NATIVE_SNAPSHOT_FILENAME, history_id=stored.history_id
        )
        if validation.state != "complete":
            raise ValueError("Incomplete or corrupt native snapshot")
    else:
        with (directory / "history.jsonl").open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                raise ValueError("Empty transcript")
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                raise ValueError("Incomplete transcript tail; source will not be reclaimed")
    return tuple(members)


def portable_digest(
    state: SpawnRecord, files: tuple[Member, ...], session: SessionRecord | None
) -> str:
    portable_state = state.model_dump(
        mode="json",
        exclude={
            "id",
            "chat_id",
            "owner_chat_id",
            "parent_id",
            "state_revision",
            "session_instance_id",
            "prompt",
            "worker_pid",
            "runner_pid",
            "runner_created_at_epoch",
            "control_root",
            "task_cwd",
            "execution_cwd",
            "claude_config_dir",
            "cancel_intent",
            "runner_exit",
            "launch_policy_snapshot",
            "originating_bash_id",
            "record_mode",
            "launch_mode",
            "harness_session_id",
            "resident_rearm_count",
        },
    )
    return digest(
        canonical(
            {
                "state": portable_state,
                "session": session.model_dump(
                    mode="json",
                    exclude={
                        "chat_id",
                        "spawn_id",
                        "history_id",
                        "session_instance_id",
                        "forked_from_chat_id",
                        "record_mode",
                        "harness_session_id",
                        "harness_session_ids",
                        "control_root",
                        "task_cwd",
                        "execution_cwd",
                        "claude_config_dir",
                    },
                )
                if session
                else None,
                "files": [
                    member.model_dump()
                    for member in files
                    if member.name not in {"state.json", "record.json"}
                ],
            }
        )
    )


def restored_record(
    directory: Path,
    files: tuple[Member, ...],
    session: SessionRecord | None,
) -> ArchivedRecord:
    """Validate local inert projections before recovering original portable facts."""
    saved = json.loads((directory / "restored-from.json").read_bytes())
    metadata = {member.name: member.sha256 for member in files}
    if (
        metadata.get("state.json") != saved["state_sha256"]
        or metadata.get("record.json") != saved["provenance_sha256"]
        or session is None
        or digest(canonical(session.model_dump(mode="json"))) != saved["session_sha256"]
    ):
        raise ValueError(f"Restored metadata changed: {directory.name}")
    original = ArchivedRecord.model_validate_json((directory / "record.json").read_bytes())
    if (
        original.portable_digest != saved["portable_digest"]
        or portable_digest(original.state, original.files, original.session)
        != original.portable_digest
    ):
        raise ValueError(f"Restored provenance changed: {directory.name}")
    excluded = {"state.json", "record.json"}
    if tuple(m for m in files if m.name not in excluded) != tuple(
        m for m in original.files if m.name not in excluded
    ):
        raise ValueError(f"Restored content changed: {directory.name}")
    return original


def capture_record(
    directory: Path,
    state: SpawnRecord,
    session: SessionRecord | None,
    activity: str,
) -> ArchivedRecord:
    if state.history_id is None:
        raise ValueError("Assign file-authoritative history identity before capture")
    if session and session.history_id not in {None, state.history_id}:
        raise ValueError("Session history identity differs from its aggregate")
    files = inventory(directory)
    stored = StoredSpawnState.model_validate_json((directory / "state.json").read_bytes())
    transcript = canonical_transcript_member(member.name for member in files)
    required = ("state.json", transcript) + (
        ("starting-prompt.md",) if stored.prompt_length is not None else ()
    )
    original = (
        restored_record(directory, files, session) if state.record_mode == "historical" else None
    )
    if original is not None:
        # A synthetic local session is not a new portable fact.
        session = original.session
        activity = original.activity
    if session is not None:
        # Bind the exported capsule only AFTER raw authority validation.
        # Discovery lookup and publication witnesses must never infer these fields.
        session = session.model_copy(
            update={
                "chat_id": state.chat_id or session.chat_id,
                "spawn_id": state.id,
                "history_id": state.history_id,
                "session_instance_id": state.session_instance_id or session.session_instance_id,
            }
        )
    portable = portable_digest(state, files, session)
    if original is not None and portable != original.portable_digest:
        raise ValueError(f"Restored portable facts changed: {state.history_id}")
    return ArchivedRecord(
        history_id=state.history_id,
        state=state,
        session=session,
        activity=activity,
        files=files,
        required_files=required,
        portable_digest=portable,
    )


def _location(destination: Path) -> UUID:
    path = destination / ".meridian-history-location.json"
    with lock_file(destination / ".meridian-history-location.lock"):
        if path.exists():
            return _LocationMarker.model_validate_json(path.read_bytes()).location_id
        location_id = uuid4()
        atomic_write_text(path, json.dumps({"version": 1, "location_id": str(location_id)}))
        return location_id


def _member_bytes(archive: zipfile.ZipFile, name: str, *, limit: int) -> bytes:
    try:
        if archive.getinfo(name).file_size > limit:
            raise ArchiveValidationError(f"Archive metadata exceeds limit: {name}")
        return archive.read(name)
    except KeyError as exc:
        raise ArchiveValidationError(f"Missing required archive member: {name}") from exc


def archive_manifest_digest(path: Path) -> str:
    """Bind later verification/extraction to the exact bounded manifest bytes."""
    try:
        with zipfile.ZipFile(path) as archive:
            return digest(_member_bytes(archive, _MANIFEST, limit=_MAX_METADATA))
    except (EOFError, zipfile.BadZipFile, zlib.error) as exc:
        raise ArchiveValidationError(f"Unreadable archive manifest: {path}: {exc}") from exc


def verify_archive(
    path: Path,
    expected: tuple[ArchivedRecord, ...] | None = None,
    *,
    full: bool = True,
    manifest_sha256: str | None = None,
    history_id: UUID | None = None,
    current: Callable[[], bool] | None = None,
) -> ArchiveManifest:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names: set[str] = set()
        total = 0
        if len(infos) > _MAX_MEMBERS:
            raise ValueError("Too many archive members")
        for info in infos:
            safe_member_name(info.filename)
            if info.filename in names:
                raise ValueError("Duplicate ZIP member")
            names.add(info.filename)
            mode = info.external_attr >> 16
            if (
                info.flag_bits & 1
                or info.is_dir()
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                raise ValueError("Unsupported ZIP member type")
            total += info.file_size
            if total > _MAX_CONTENT or info.file_size > _MAX_CONTENT:
                raise ValueError("ZIP exceeds content limit")
            if info.file_size > max(1024 * 1024, info.compress_size * 10_000):
                raise ValueError("Suspicious ZIP compression ratio")
        metadata = _member_bytes(archive, _MANIFEST, limit=_MAX_METADATA)
        if manifest_sha256 is not None and digest(metadata) != manifest_sha256:
            raise ArchiveValidationError("Archive manifest does not match its receipt")
        manifest = ArchiveManifest.model_validate_json(metadata)
        if len({record.history_id for record in manifest.records}) != len(manifest.records):
            raise ValueError("Duplicate portable history identity")
        declared = {member.name: member for member in manifest.members}
        if len(declared) != len(manifest.members) or names != set(declared) | {_MANIFEST}:
            raise ValueError("ZIP member coverage does not match manifest")
        planned_names: set[str] = set()
        for record in manifest.records:
            prefix = f"{_PREFIX}records/{record.history_id}/"
            meta_name = prefix + "record.json"
            planned_names.add(meta_name)
            if len({m.name for m in record.files}) != len(record.files):
                raise ValueError("Duplicate record inventory member")
            recovered = ArchivedRecord.model_validate_json(
                _member_bytes(archive, meta_name, limit=_MAX_METADATA)
            )
            if (
                portable_digest(record.state, record.files, record.session)
                != record.portable_digest
            ):
                raise ValueError("Portable record digest mismatch")
            if recovered != record or record.state.history_id != record.history_id:
                raise ValueError("Record metadata identity mismatch")
            stored = StoredSpawnState.model_validate_json(
                _member_bytes(archive, prefix + "aggregate/state.json", limit=_MAX_METADATA)
            )
            transcript = canonical_transcript_member(m.name for m in record.files)
            required = {"state.json", transcript}
            if stored.prompt_length is not None:
                required.add("starting-prompt.md")
            if set(record.required_files) != required or not required <= {
                m.name for m in record.files
            }:
                raise ValueError(
                    "Required record membership does not match authoritative references"
                )
            if stored.model_dump(exclude={"prompt_length"}) != record_to_stored_state(
                record.state
            ).model_dump(exclude={"prompt_length"}):
                raise ValueError("Record metadata does not describe its authoritative state")
            if record.session and record.session.history_id not in {None, record.history_id}:
                raise ValueError("Session history identity differs from its aggregate")
            if record.session and record.session.spawn_id != record.state.id:
                raise ValueError("Session metadata belongs to a different record")
            transcript_member = prefix + "aggregate/" + transcript
            if transcript_member not in names:
                raise ArchiveValidationError("Missing required archive transcript")
            with archive.open(transcript_member) as handle:
                first = handle.readline(_MAX_METADATA + 1)
                if len(first) > _MAX_METADATA:
                    raise ValueError("Transcript first record exceeds metadata limit")
                header = json.loads(first)
                if (
                    isinstance(header, dict)
                    and header.get("record") == "meridian.transcript"
                    and TranscriptHeader.model_validate(header).history_id != record.history_id
                ):
                    raise ValueError("Transcript identity does not match selected record")
                if (
                    isinstance(header, dict)
                    and header.get("record") == "meridian.native.snapshot"
                    and TranscriptHeader.model_validate(header.get("transcript")).history_id
                    != record.history_id
                ):
                    raise ValueError("Transcript identity does not match selected record")
            for member in record.files:
                relative = safe_member_name(member.name)
                if (
                    PurePosixPath(relative).name in _EXCLUDED
                    or PurePosixPath(relative).suffix in {".lock", ".sock", ".sentinel"}
                    or _is_reserved_atomic_temp(PurePosixPath(relative).name)
                ):
                    raise ValueError("Runtime-control member in portable archive")
                name = prefix + "aggregate/" + safe_member_name(member.name)
                planned_names.add(name)
                if declared.get(name) != member.model_copy(update={"name": name}):
                    raise ValueError("Record inventory is not covered by archive")
        if planned_names != set(declared):
            raise ValueError("Archive contains members outside the record selection")
        # Independent capture-plan comparison prevents a self-consistent omission.
        if expected is not None and manifest.records != expected:
            raise ValueError("ZIP does not cover the selected source snapshots")
        if history_id is not None and not any(
            record.history_id == history_id for record in manifest.records
        ):
            raise ArchiveValidationError("Selected history is not covered by the ZIP")
        for member in manifest.members if full else ():
            if history_id is not None and not member.name.startswith(
                f"{_PREFIX}records/{history_id}/"
            ):
                continue
            checksum = hashlib.sha256()
            size = 0
            with archive.open(member.name) as handle:
                while chunk := handle.read(1024 * 1024):
                    if current is not None and not current():
                        raise InterruptedError("Archive verification cancelled")
                    checksum.update(chunk)
                    size += len(chunk)
            if size != member.size or checksum.hexdigest() != member.sha256:
                raise ValueError(f"ZIP checksum/size mismatch: {member.name}")
        return manifest


def append_receipt(root: Path, receipt: ArchiveReceipt) -> None:
    changes = HistoryChanges(root)
    source = HistorySource(kind="catalog")
    with lock_file(changes.mutation_lock, mode="shared"), lock_file(source.lock_path(root)):
        receipts = read_receipts(root)
        for existing in receipts:
            if (
                existing.archive_id == receipt.archive_id
                and existing.manifest_sha256 != receipt.manifest_sha256
            ):
                raise ValueError("Archive identity conflicts with an existing receipt")
        if receipt in receipts:
            heads = catalog_heads(receipts)
            if receipt.event != "imported" or all(
                heads.get(str(record.history_id)) == record.portable_digest
                for record in receipt.records
            ):
                return
        changes.mark(source)
        append_durable_jsonl_line(
            root / "history-archives/catalog.jsonl", receipt.model_dump_json() + "\n"
        )


def read_receipts(root: Path) -> tuple[ArchiveReceipt, ...]:
    path = root / "history-archives/catalog.jsonl"
    if not path.exists():
        return ()
    receipts: list[ArchiveReceipt] = []
    with path.open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                break
            receipts.append(ArchiveReceipt.model_validate_json(line))
    return tuple(receipts)


def catalog_heads(receipts: tuple[ArchiveReceipt, ...]) -> dict[str, str]:
    """Current snapshot selection is authority evidence, not newest ZIP ordering."""
    heads: dict[str, str] = {}
    for receipt in receipts:
        if receipt.event in {"reclaim_prepared", "reclaimed", "imported"}:
            for record in receipt.records:
                heads[str(record.history_id)] = record.portable_digest
    return heads


def publish_archive(
    root: Path,
    destination: Path,
    records: tuple[ArchivedRecord, ...],
) -> ArchiveReceipt:
    with lock_file(root / "history-archives/archive.lock"):
        if not records:
            raise ValueError("No records selected")
        destination.mkdir(parents=True, exist_ok=True)
        location_id = _location(destination)
        for existing in reversed(read_receipts(root)):
            if existing.records != records:
                continue
            try:
                path = archive_locations((existing,), destination=destination, full=True)[0].path
                verify_archive(path, records)
            except ARCHIVE_READ_ERRORS:
                continue
            reused = existing.model_copy(update={"destination": str(path.parent)})
            append_receipt(root, reused)
            return reused
        archive_id = uuid4()
        name = f"meridian-history-{archive_id}.zip"
        # One unpublished ZIP per runtime, serialized by archive.lock.
        stage = destination / f".partial-{digest(str(root.resolve()).encode())}"
        stage.unlink(missing_ok=True)
        members: list[Member] = []
        try:
            stage.touch(mode=0o600, exist_ok=False)
            with zipfile.ZipFile(
                stage, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for record in records:
                    prefix = f"{_PREFIX}records/{record.history_id}/"
                    metadata = record.model_dump_json().encode()
                    archive.writestr(prefix + "record.json", metadata)
                    members.append(
                        Member(
                            name=prefix + "record.json", size=len(metadata), sha256=digest(metadata)
                        )
                    )
                    source = HistorySource(kind="spawn", key=record.state.id)
                    with lock_file(source.lock_path(root)):
                        directory = root / "spawns" / record.state.id
                        if inventory(directory) != record.files:
                            raise ValueError("Source changed before archive capture")
                        for member in record.files:
                            target = prefix + "aggregate/" + member.name
                            archive.write(directory / member.name, target)
                            members.append(member.model_copy(update={"name": target}))
                manifest = ArchiveManifest(
                    archive_id=archive_id,
                    created_at=utc_now_iso(),
                    records=records,
                    members=tuple(members),
                )
                archive.writestr(_MANIFEST, manifest.model_dump_json().encode())
            with stage.open("rb") as handle:
                os.fsync(handle.fileno())
            verified = verify_archive(stage, records)
            # Hard-link publication is exclusive; no replace can overwrite an existing ZIP.
            final = destination / name
            os.link(stage, final)
            fsync_directory(destination)
        finally:
            stage.unlink(missing_ok=True)
        manifest_hash = archive_manifest_digest(final)
        receipt = ArchiveReceipt(
            event="published",
            archive_id=verified.archive_id,
            location_id=location_id,
            destination=str(destination),
            zip_name=name,
            manifest_sha256=manifest_hash,
            records=records,
        )
        append_receipt(root, receipt)
        return receipt


def archive_path(receipt: ArchiveReceipt, destination: Path | None = None) -> Path:
    directory = destination or Path(receipt.destination)
    marker = directory / ".meridian-history-location.json"
    if not marker.exists():
        raise FileNotFoundError(f"Archive destination offline: {directory}")
    if _LocationMarker.model_validate_json(marker.read_bytes()).location_id != receipt.location_id:
        raise ValueError("Archive destination identity does not match receipt")
    safe_member_name(receipt.zip_name)
    if "/" in receipt.zip_name:
        raise ValueError("Archive filename must be a basename")
    return directory / receipt.zip_name


def archive_display_path(receipt: ArchiveReceipt, destination: Path | None = None) -> Path:
    """Display the configured remount when its location identity matches."""
    if destination is not None:
        try:
            return archive_path(receipt, destination)
        except (ValueError, OSError):
            pass
    return Path(receipt.destination) / receipt.zip_name


def archive_locations(
    receipts: Iterable[ArchiveReceipt],
    *,
    destination: Path | None = None,
    full: bool = False,
    deadline: float | None = None,
) -> tuple[ArchiveLocation, ...]:
    """Resolve only the supplied selection, checking every location against its receipt.

    Callers select a history digest or an explicit archive UUID. This module owns
    physical availability, remount hints and verification, never currentness.
    """
    available: list[ArchiveLocation] = []
    errors: list[str] = []
    seen: set[tuple[Path, UUID, str]] = set()
    for receipt in receipts:
        directories = (
            (destination, Path(receipt.destination))
            if destination
            else (Path(receipt.destination),)
        )
        for directory in directories:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Archive location resolution exceeded the query budget")
            key = (directory / receipt.zip_name, receipt.location_id, receipt.manifest_sha256)
            if key in seen:
                continue
            seen.add(key)
            try:
                path = archive_path(receipt, directory)
                verify_archive(path, full=full, manifest_sha256=receipt.manifest_sha256)
                available.append(ArchiveLocation(receipt, path))
            except ARCHIVE_READ_ERRORS as exc:
                errors.append(f"{directory / receipt.zip_name}: {exc}")
    if not available:
        raise ArchiveUnavailable("; ".join(errors) or "No archive location is registered")
    return tuple(available)


class _HashingMemberReader:
    """Hash and count a ZIP member while a streaming codec reads from it."""

    def __init__(self, handle: IO[bytes]) -> None:
        self._handle = handle
        self.checksum = hashlib.sha256()
        self.size = 0

    def readline(self, size: int | None = -1, /) -> bytes:
        chunk = self._handle.readline(size if size is not None else -1)
        if chunk:
            self.checksum.update(chunk)
            self.size += len(chunk)
        return chunk

    def tell(self) -> int:
        return self._handle.tell()


def iter_archived_events(
    path: Path, history_id: UUID, manifest_sha256: str | None = None
) -> Iterator[dict[str, object]]:
    """Stream one verified member without reading/extracting other transcript bodies."""
    manifest = verify_archive(path, full=False, manifest_sha256=manifest_sha256)
    record = next((r for r in manifest.records if r.history_id == history_id), None)
    if record is None:
        raise ValueError(f"Archive has no transcript for {history_id}")
    member_name = canonical_transcript_member(m.name for m in record.files)
    name = f"{_PREFIX}records/{history_id}/aggregate/{member_name}"
    expected = next((member for member in manifest.members if member.name == name), None)
    if expected is None:
        raise ValueError(f"Archive has no transcript for {history_id}")
    checksum = hashlib.sha256()
    size = 0
    with zipfile.ZipFile(path) as archive:
        if (
            manifest_sha256
            and digest(_member_bytes(archive, _MANIFEST, limit=_MAX_METADATA)) != manifest_sha256
        ):
            raise ValueError("Archive manifest does not match published receipt")
        with archive.open(name) as handle:
            if member_name == NATIVE_SNAPSHOT_FILENAME:
                reader = _HashingMemberReader(handle)
                validation = TranscriptValidation()
                yield from read_snapshot(reader, validation=validation)
                if validation.state != "complete":
                    raise ValueError(validation.reason or "Archived native snapshot is incomplete")
                checksum = reader.checksum
                size = reader.size
            else:
                for line in handle:
                    checksum.update(line)
                    size += len(line)
                    if not line.endswith(b"\n"):
                        raise ValueError("Archived transcript has an incomplete tail")
                    payload = json.loads(line)
                    if isinstance(payload, dict) and payload.get("record") != "meridian.transcript":
                        yield payload
        if size != expected.size or checksum.hexdigest() != expected.sha256:
            raise ValueError("Archived transcript checksum mismatch")


def import_archive(root: Path, path: Path, *, select: bool = True) -> ArchiveReceipt:
    """Register a verified ZIP for direct reads; never extract or start anything."""
    path = path.expanduser().resolve()
    with lock_file(root / "history-archives/archive.lock"):
        manifest_hash = archive_manifest_digest(path)
        manifest = verify_archive(path, manifest_sha256=manifest_hash)
        location_id = _location(path.parent)
        receipt = ArchiveReceipt(
            event="imported" if select else "published",
            archive_id=manifest.archive_id,
            location_id=location_id,
            destination=str(path.parent),
            zip_name=path.name,
            manifest_sha256=manifest_hash,
            records=manifest.records,
        )
        append_receipt(root, receipt)
        return receipt


def recover_archives(root: Path, destination: Path) -> tuple[str, ...]:
    """Recover independent receipts; unavailable copies do not stall unrelated work."""
    from meridian.lib.state.spawn.repository import read_state
    from meridian.lib.state.spawn_aggregate import sync_retirement_parents

    errors: list[str] = []
    with lock_file(root / "history-archives/archive.lock"):
        receipts = read_receipts(root)
        if destination.is_dir():
            # Only this runtime owns this unpublished path; other runtimes may publish here.
            (destination / f".partial-{digest(str(root.resolve()).encode())}").unlink(
                missing_ok=True
            )
            location_id = _location(destination)
            known = {(row.zip_name, row.location_id) for row in receipts}
            for path in sorted(destination.glob("meridian-history-*.zip")):
                if (path.name, location_id) not in known:
                    try:
                        import_archive(root, path, select=False)
                    except ARCHIVE_READ_ERRORS as exc:
                        errors.append(f"{path}: {exc}")
        receipts = read_receipts(root)
        heads = catalog_heads(receipts)
        for receipt in receipts:
            if receipt.event != "reclaim_prepared":
                continue
            for record in receipt.records:
                if heads.get(str(record.history_id)) != record.portable_digest:
                    continue
                completed = receipt.model_copy(update={"event": "reclaimed", "records": (record,)})
                if completed in receipts:
                    continue
                changes = HistoryChanges(root)
                source = HistorySource(kind="spawn", key=record.state.id)
                with (
                    lock_file(changes.mutation_lock, mode="shared"),
                    lock_file(source.lock_path(root)),
                ):
                    if (
                        read_state(root / "spawns", record.state.id, include_prompt=False)
                        is not None
                    ):
                        continue
                try:
                    copies = (
                        row for row in reversed(receipts) if row.archive_id == receipt.archive_id
                    )
                    location = archive_locations(copies, destination=destination, full=True)[0]
                    manifest = verify_archive(
                        location.path, manifest_sha256=receipt.manifest_sha256
                    )
                    if record not in manifest.records:
                        raise ArchiveValidationError("Prepared reclaim is not covered by its ZIP")
                except ARCHIVE_READ_ERRORS as exc:
                    errors.append(f"{record.history_id}: recovery deferred: {exc}")
                    continue
                with (
                    lock_file(changes.mutation_lock, mode="shared"),
                    lock_file(source.lock_path(root)),
                ):
                    if read_state(root / "spawns", record.state.id, include_prompt=False) is None:
                        try:
                            sync_retirement_parents(root / "spawns")
                        except OSError as exc:
                            errors.append(f"{record.history_id}: recovery deferred: {exc}")
                            continue
                        append_receipt(root, completed)
    return tuple(errors)
