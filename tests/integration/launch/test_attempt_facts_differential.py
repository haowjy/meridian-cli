"""PR 2 differential gate; delete the frozen oracle and this file in PR 3."""

import importlib
import json
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeKey
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections.base import ConnectionConfig, RawHarnessEvent
from meridian.lib.launch.extract import enrich_finalize
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.artifact_store import InMemoryStore, make_artifact_key
from meridian.lib.streaming.drain_policy import PersistentDrainPolicy
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.legacy_f1b.extract import enrich_finalize as old_finalize
from tests.support.opencode_db import write_opencode_v2_db_session
from tests.support.pi import NoopControlServer, start_row
from tests.support.resident_drain import FakeResidentConnection

CASES = {
    "claude": [
        {"type": "system", "session_id": "native-owned"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "draft"}]}},
        {
            "type": "result",
            "result": "final report",
            "usage": {
                "model": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                }
            },
            "total_cost_usd": 0.3,
        },
    ],
    "codex": [
        {"type": "thread.started", "thread_id": "native-owned"},
        {"type": "turn/started", "threadId": "native-owned"},
        {
            "type": "item/completed",
            "threadId": "native-owned",
            "item": {"type": "agentMessage", "text": "final report"},
        },
        {"type": "turn/completed", "usage": {"input_tokens": 10, "output_tokens": 20}},
    ],
    "opencode": [
        {
            "type": "message.updated",
            "properties": {"info": {"sessionID": "ses_fixture", "id": "u", "role": "user"}},
        },
        {
            "type": "message.updated",
            "properties": {
                "info": {
                    "sessionID": "ses_fixture",
                    "id": "a",
                    "role": "assistant",
                    "tokens": {"input": 10, "output": 20},
                    "cost": 0.3,
                }
            },
        },
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "sessionID": "ses_fixture",
                    "messageID": "a",
                    "type": "text",
                    "text": "final report",
                }
            },
        },
        {"type": "session.idle", "properties": {"sessionID": "ses_fixture"}},
    ],
    "opencode_v2": [
        {
            "type": "session.text.ended",
            "sessionID": "ses_fixture",
            "assistantMessageID": "ses_fixture_msg_0",
            "text": "stream fallback",
        },
    ],
    "pi": [
        {"type": "session", "id": "native-owned"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "usage": {
                    "input": 10,
                    "output": 20,
                    "cacheRead": 3,
                    "cacheWrite": 4,
                    "cost": {"total": 0.3},
                },
            },
        },
        {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "final report"}]}
            ],
        },
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_live_fold_equals_same_run_artifact_oracle(
    tmp_path: Path, monkeypatch, request, case
):
    harness = HarnessId(case.split("_")[0])
    spawn = SpawnId("p1")
    start_row(tmp_path, str(spawn), harness, None)
    connection = FakeResidentConnection(harness)
    facts = AttemptFacts()
    extractor = get_harness_bundle(harness).extractor
    db = tmp_path / "opencode.db"
    if case == "opencode_v2":
        write_opencode_v2_db_session(
            db_path=db,
            session_id="ses_fixture",
            messages=[("assistant", {"content": [{"type": "text", "text": "final report"}]})],
        )
        monkeypatch.setenv("OPENCODE_DB", str(db))
    else:
        monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "absent.db"))

    async def start(config, spec):
        await connection.start(config, spec)
        return connection

    manager = SpawnManager(
        runtime_root=tmp_path,
        project_root=tmp_path,
        start_connection=start,
        control_server_factory=lambda *_args: NoopControlServer(),
    )
    try:
        await manager.start_spawn(
            ConnectionConfig(
                spawn_id=spawn,
                harness_id=harness,
                prompt="test",
                control_root=tmp_path,
                child_env={},
            ),
            ResolvedLaunchSpec(
                harness=str(harness),
                prompt="test",
                permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            ),
            drain_policy=PersistentDrainPolicy(),
            event_hook=lambda event: facts.hook(extractor, event),
        )
        for payload in CASES[case]:
            connection.emit(
                RawHarnessEvent(
                    event_type=payload["type"], harness_id=str(harness), payload=payload
                )
            )
        connection.close_stream()
        await manager.wait_for_completion(spawn)
    finally:
        await manager.stop_spawn(spawn)

    oracle_artifacts = InMemoryStore()
    if request.config.getoption("--runner-history") == "off":
        # Same frames, in memory: no writer construction or history reads in this mode.
        history = "".join(
            json.dumps({"event_type": p["type"], "payload": p}) + "\n" for p in CASES[case]
        ).encode()
    else:
        history = (tmp_path / "spawns" / spawn / "history.jsonl").read_bytes()
    oracle_artifacts.put(make_artifact_key(spawn, "history.jsonl"), history)
    old_extractor = getattr(
        importlib.import_module(f"tests.support.legacy_f1b.{harness}"),
        f"{str(harness).upper()}_EXTRACTOR",
    )
    expected = old_finalize(
        artifacts=oracle_artifacts,
        extractor=old_extractor,
        spawn_id=spawn,
        log_dir=tmp_path / "old",
    )
    actual = enrich_finalize(
        facts=facts,
        extractor=extractor,
        native_key=NativeKey(str(harness), str(db), "ses_fixture"),
        artifacts=InMemoryStore(),
        spawn_id=spawn,
        log_dir=tmp_path / "new",
    )
    # F11 gives the old all-unknown usage value its explicit None representation.
    from meridian.lib.core.domain import TokenUsage

    if expected.usage == TokenUsage():
        expected = expected.model_copy(update={"usage": None})
    assert actual.model_dump(exclude={"report_path"}) == expected.model_dump(
        exclude={"report_path"}
    )
