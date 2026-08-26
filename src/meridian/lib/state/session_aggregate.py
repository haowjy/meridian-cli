"""Cross-store projections composed from authoritative session and spawn state."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.state import session_index, spawn_store
from meridian.lib.state.paths import RuntimePaths


def primary_spawn_generation(runtime_root: Path) -> str:
    """Return an O(1) generation for primary-spawn relationship discovery."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    try:
        directory = paths.spawns_dir.stat()
        directory_state = (
            directory.st_dev,
            directory.st_ino,
            directory.st_size,
            directory.st_mtime_ns,
            directory.st_ctime_ns,
        )
    except FileNotFoundError:
        directory_state = (0, 0, 0, 0, 0)
    counter_path = paths.root_dir / "spawn-id-counter"
    try:
        counter = counter_path.read_text(encoding="utf-8").strip()
    except OSError:
        counter = ""
    return ":".join((*map(str, directory_state), counter))


def backfill_primary_spawn_ids(
    runtime_root: Path,
    payloads: list[session_index.SessionPayload],
) -> session_index.SessionBackfillResult:
    """Join legacy primary sessions to spawn rows without creating a leaf cycle."""

    generation_before = primary_spawn_generation(runtime_root)
    missing_chat_ids = {
        chat_id
        for payload in payloads
        if not payload.get("spawn_id")
        and isinstance((chat_id := payload.get("chat_id")), str)
    }
    primary_spawns: dict[str, str] = {}
    if missing_chat_ids:
        for spawn in spawn_store.list_spawns(runtime_root).records:
            owner_chat_id = spawn.owner_chat_id or spawn.chat_id
            if (
                spawn.kind == "primary"
                and owner_chat_id is not None
                and owner_chat_id in missing_chat_ids
            ):
                primary_spawns[owner_chat_id] = spawn.id
    generation_after = primary_spawn_generation(runtime_root)
    enriched = [
        {**payload, "spawn_id": primary_spawns[chat_id]}
        for payload in payloads
        if not payload.get("spawn_id")
        and isinstance((chat_id := payload.get("chat_id")), str)
        and chat_id in primary_spawns
    ]
    return session_index.SessionBackfillResult(
        payloads=enriched,
        generation_before=generation_before,
        generation_after=generation_after,
    )


PRIMARY_SPAWN_BACKFILL = session_index.SessionBackfill(
    generation=primary_spawn_generation,
    enrich=backfill_primary_spawn_ids,
)


__all__ = ["PRIMARY_SPAWN_BACKFILL"]
