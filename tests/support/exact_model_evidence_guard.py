"""Run a minimal exact-provider acceptance check behind a child-local guard."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="meridian-exact-evidence-home-"))
for key in tuple(os.environ):
    if key.startswith(("MERIDIAN", "_MERIDIAN", "PI_", "CODEX", "CLAUDE", "XDG", "MARS")):
        os.environ.pop(key, None)
for key in ("HOME", "MERIDIAN_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
    os.environ[key] = str(home)

denied: list[str] = []


def deny_external(event: str, _args: tuple[object, ...]) -> None:
    if event.startswith(
        ("subprocess.", "socket.", "os.exec", "os.spawn", "os.posix_spawn")
    ) or event in {"os.system", "os.fork", "os.forkpty"}:
        denied.append(event)
        raise RuntimeError(f"guard denied {event}")


sys.addaudithook(deny_external)
for self_test in (
    lambda: subprocess.Popen(["/nonexistent-meridian-denial-self-test"]),
    lambda: socket.socket(),
    lambda: sys.audit("os.fork"),
):
    try:
        self_test()
    except RuntimeError:
        continue
    raise AssertionError("process/network denial guard did not block its self-test")
if len(denied) != 3:
    raise AssertionError(f"expected three blocked guard self-tests, got {denied!r}")
denied.clear()

from meridian.lib.harness.model_observation import read_model_evidence_exact  # noqa: E402
from meridian.lib.harness.pi_native_source import PiSourceQualified, qualify_pi_source  # noqa: E402
from meridian.lib.state.session_authority import (  # noqa: E402
    ExactModelObservation,
    NativeSessionKey,
    NativeSourceRef,
    RecordedNativeSource,
)

store = home / "synthetic-store"
store.mkdir()
path = store / "session.jsonl"
path.write_text(
    json.dumps({"type": "session", "version": 3, "id": "synthetic-id", "cwd": "/synthetic"})
    + "\n"
    + json.dumps(
        {"type": "model_change", "id": "entry", "parentId": None, "provider": "p", "modelId": "m"}
    ),
    encoding="utf-8",
)
qualified = qualify_pi_source(
    effective_store=store, session_id="synthetic-id", session_file=str(path)
)
if not isinstance(qualified, PiSourceQualified):
    raise AssertionError(f"synthetic source did not qualify: {qualified!r}")
source = RecordedNativeSource(
    ref=NativeSourceRef(chat_id="chat", binding_event_id="a" * 64, locator_event_id="b" * 64),
    key=NativeSessionKey(harness="pi", store=str(store), native_session_id="synthetic-id"),
    locator=qualified.observation,
)
observation = read_model_evidence_exact(source)
if not isinstance(observation, ExactModelObservation) or observation.model_token != "m":
    raise AssertionError(f"exact synthetic provider acceptance failed: {observation!r}")
if denied:
    raise AssertionError(f"provider attempted a denied external operation: {denied!r}")
print('{"accepted": true, "denied_self_tests": 3, "external_attempts": 0}')
