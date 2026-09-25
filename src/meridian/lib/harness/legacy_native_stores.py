"""Bounded legacy candidates. No ambient harness config or cross-project search."""

from __future__ import annotations

import shutil
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from meridian.lib.core.native_identity import NativeEntryMismatch, NativeSessionUnavailable
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.codex_rollout import CODEX_ROLLOUT_FILENAME_RE, resolve_exact_rollout
from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.platform import get_home_path
from meridian.lib.state.session_store import SessionRecord
from meridian.lib.state.spawn.model import SpawnRecord

SUPPORTED = frozenset({"claude", "codex", "opencode", "pi"})


class LegacyNativeStores:
    """One import's Codex inventory; other harness candidates are direct paths."""

    def __init__(self, scratch: ExitStack) -> None:
        self._scratch = scratch
        self._opencode: dict[Path, Path] = {}
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
                Path(cast("str", adapter.native_store_for_launch(child_env=env, child_cwd=cwd)))
                for cwd in cwds
            }
        if chat.harness == "pi":
            return {(resolve_pi_spawn_session_root() / spawn.id).resolve() for spawn in spawns}
        stores: set[Path] = set()
        for env in envs or [{}]:
            recorded_env = {"HOME": str(get_home_path()), **env}
            # Relative recorded config is relative to a recorded launch cwd only.
            for cwd in cwds or {get_home_path()}:
                stores.add(
                    Path(
                        cast(
                            "str",
                            adapter.native_store_for_launch(
                                child_env=recorded_env.copy(),
                                child_cwd=cwd,
                            ),
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
                    exact_store = store
                    if chat.harness == "opencode" and store.is_file():
                        if store not in self._opencode:
                            # SQLite mode=ro can still write WAL shared-memory read marks.
                            # Copy DB + committed WAL bytes, never open the source via SQLite.
                            directory = Path(
                                self._scratch.enter_context(
                                    TemporaryDirectory(
                                        prefix="meridian-native-import-",
                                    )
                                )
                            )
                            copied = directory / store.name
                            before = _opencode_signature(store)
                            shutil.copyfile(store, copied)
                            wal = store.with_name(store.name + "-wal")
                            if before[1] is not None:
                                shutil.copyfile(wal, copied.with_name(copied.name + "-wal"))
                            if _opencode_signature(store) != before:
                                raise OSError(
                                    f"OpenCode store changed during legacy import: {store}; "
                                    "retry when its writer is idle"
                                )
                            self._opencode[store] = copied
                        exact_store = self._opencode[store]
                    source = adapter.resolve_native_session_file(
                        project_root=Path(chat.control_root or chat.execution_cwd or "/"),
                        session_id=session_id,
                        native_store=exact_store,
                    )
                if source is not None:
                    matches.add(store)
            except NativeEntryMismatch:
                continue
            except NativeSessionUnavailable as exc:
                ambiguous |= exc.reason == "ambiguous_native_file"
        return matches, ambiguous


def _opencode_signature(
    store: Path,
) -> tuple[tuple[int, int, int], tuple[int, int, int, bytes] | None]:
    """Detect checkpoints/restarts while copying without opening SQLite on the source."""
    db = store.stat()
    wal_path = store.with_name(store.name + "-wal")
    try:
        with wal_path.open("rb") as handle:
            wal = wal_path.stat()
            wal_signature = (wal.st_ino, wal.st_size, wal.st_mtime_ns, handle.read(32))
    except FileNotFoundError:
        wal_signature = None
    return (db.st_ino, db.st_size, db.st_mtime_ns), wal_signature
