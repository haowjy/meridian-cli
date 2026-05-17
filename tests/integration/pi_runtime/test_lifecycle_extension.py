# qa-validated: pi-rpc-quiescence
"""Executable fake-Pi tests for the Meridian lifecycle extension contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LIFECYCLE_DIST = (
    ROOT
    / "src"
    / "meridian"
    / "pi_runtime"
    / "dist"
    / "extensions"
    / "meridian-lifecycle"
    / "index.js"
)


def _run_node_harness(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for Pi runtime extension harness tests")
    if not LIFECYCLE_DIST.is_file():
        pytest.skip("lifecycle extension dist artifact is not built")

    script = tmp_path / "lifecycle-harness.mjs"
    script.write_text(source, encoding="utf-8")
    env = {
        **os.environ,
        "MERIDIAN_LIFECYCLE_EXTENSION": str(LIFECYCLE_DIST),
        "MERIDIAN_TEST_TMP": str(tmp_path),
    }
    result = subprocess.run(
        [node, str(script)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    marker_lines = [
        line.removeprefix("@@RESULT@@")
        for line in result.stdout.splitlines()
        if line.startswith("@@RESULT@@")
    ]
    assert marker_lines, result.stdout
    return json.loads(marker_lines[-1])


def test_lifecycle_child_drain_queues_delivers_and_completes_notification(tmp_path: Path) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
const originalWrite = process.stdout.write.bind(process.stdout);
const rawWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  rawWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};

const handlers = new Map();
const internalHandlers = new Map();
const sentMessages = [];
function addHandler(map, name, cb) {
  const list = map.get(name) ?? [];
  list.push(cb);
  map.set(name, list);
  return () => map.set(name, (map.get(name) ?? []).filter((item) => item !== cb));
}
async function emit(map, name, ...args) {
  for (const cb of map.get(name) ?? []) await cb(...args);
}
const pi = {
  on(name, cb) { return addHandler(handlers, name, cb); },
  events: {
    on(name, cb) { return addHandler(internalHandlers, name, cb); },
    emit(name, payload) { void emit(internalHandlers, name, payload); },
  },
  sendMessage(message, options) { sentMessages.push({ message, options }); },
};
const { default: lifecycle } = await import(process.env.MERIDIAN_LIFECYCLE_EXTENSION);
lifecycle(pi);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "j-1", wait_policy: "tracked", kind: "bash" },
);
await emit(handlers, "agent_end", {});
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  { subspawn_id: "j-1", wait_policy: "tracked", kind: "bash", success: true },
);
await new Promise((resolve) => setTimeout(resolve, 0));
await emit(handlers, "agent_start", {});
await emit(handlers, "agent_end", {});
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents }) + "\n");
''',
    )

    event_types = [event["type"] for event in output["lifecycleEvents"]]
    assert "meridian.notification.queued" in event_types
    assert "meridian.notification.delivered" in event_types
    assert "meridian.notification.completed" in event_types
    sent = output["sentMessages"]
    assert sent[0]["options"] == {"deliverAs": "followUp", "triggerTurn": True}


def test_lifecycle_send_message_failure_emits_stable_failure_reason(tmp_path: Path) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
const originalWrite = process.stdout.write.bind(process.stdout);
const rawWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  rawWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};
const handlers = new Map();
const internalHandlers = new Map();
function addHandler(map, name, cb) {
  const list = map.get(name) ?? [];
  list.push(cb);
  map.set(name, list);
  return () => undefined;
}
async function emit(map, name, ...args) {
  for (const cb of map.get(name) ?? []) await cb(...args);
}
const pi = {
  on(name, cb) { return addHandler(handlers, name, cb); },
  events: {
    on(name, cb) { return addHandler(internalHandlers, name, cb); },
    emit(name, payload) { void emit(internalHandlers, name, payload); },
  },
  sendMessage() { throw new Error("boom"); },
};
const { default: lifecycle } = await import(process.env.MERIDIAN_LIFECYCLE_EXTENSION);
lifecycle(pi);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "j-1", wait_policy: "tracked", kind: "bash" },
);
await emit(handlers, "agent_end", {});
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  { subspawn_id: "j-1", wait_policy: "tracked", kind: "bash", success: true },
);
await new Promise((resolve) => setTimeout(resolve, 0));
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({ lifecycleEvents }) + "\n");
''',
    )

    failed = [
        event
        for event in output["lifecycleEvents"]
        if event["type"] == "meridian.notification.failed"
    ]
    assert len(failed) == 1
    assert failed[0]["reason"] == "sendMessage_error"
    assert failed[0]["error"] == "boom"


def test_lifecycle_primary_role_emits_quiescence_ready_without_parent_resume(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "primary";
const originalWrite = process.stdout.write.bind(process.stdout);
const rawWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  rawWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};
const handlers = new Map();
const internalHandlers = new Map();
const sentMessages = [];
function addHandler(map, name, cb) {
  const list = map.get(name) ?? [];
  list.push(cb);
  map.set(name, list);
  return () => undefined;
}
async function emit(map, name, ...args) {
  for (const cb of map.get(name) ?? []) await cb(...args);
}
const pi = {
  on(name, cb) { return addHandler(handlers, name, cb); },
  events: {
    on(name, cb) { return addHandler(internalHandlers, name, cb); },
    emit(name, payload) { void emit(internalHandlers, name, payload); },
  },
  sendMessage(message, options) { sentMessages.push({ message, options }); },
};
const { default: lifecycle } = await import(process.env.MERIDIAN_LIFECYCLE_EXTENSION);
lifecycle(pi);
await emit(handlers, "agent_end", {});
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents }) + "\n");
''',
    )

    ready_events = [
        event
        for event in output["lifecycleEvents"]
        if event["type"] == "meridian.quiescence.ready"
    ]
    assert len(ready_events) == 1
    assert ready_events[0]["role"] == "primary"
    assert ready_events[0]["tracked_count"] == 0
    assert ready_events[0]["pending_notification_count"] == 0
    assert output["sentMessages"] == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="uses a POSIX shell stub/chmod/PATH shim for meridian wrapper interception",
)
def test_lifecycle_meridian_spawn_wrapper_log_handoff_tracks_child_spawn_id(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
const fs = await import("node:fs/promises");
const path = await import("node:path");

const testTmp = process.env.MERIDIAN_TEST_TMP;
const binDir = path.join(testTmp, "bin");
await fs.mkdir(binDir, { recursive: true });
const meridian = path.join(binDir, "meridian");
await fs.writeFile(
  meridian,
  "#!/bin/sh\nprintf '%s\\n' '{\"status\":\"succeeded\"}'\n",
  "utf-8",
);
await fs.chmod(meridian, 0o755);
process.env.PATH = binDir + ":" + process.env.PATH;

const wrapperLog = path.join(testTmp, "wrapper.log");
await fs.writeFile(wrapperLog, "launched Meridian spawn p123\n", "utf-8");

const originalWrite = process.stdout.write.bind(process.stdout);
const rawWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  rawWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};
const handlers = new Map();
const internalHandlers = new Map();
function addHandler(map, name, cb) {
  const list = map.get(name) ?? [];
  list.push(cb);
  map.set(name, list);
  return () => map.set(name, (map.get(name) ?? []).filter((item) => item !== cb));
}
async function emit(map, name, ...args) {
  for (const cb of map.get(name) ?? []) await cb(...args);
}
const sentMessages = [];
const pi = {
  on(name, cb) { return addHandler(handlers, name, cb); },
  events: {
    on(name, cb) { return addHandler(internalHandlers, name, cb); },
    emit(name, payload) { void emit(internalHandlers, name, payload); },
  },
  sendMessage(message, options) { sentMessages.push({ message, options }); },
};
const { default: lifecycle } = await import(process.env.MERIDIAN_LIFECYCLE_EXTENSION);
lifecycle(pi);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  {
    subspawn_id: "j-wrap",
    wait_policy: "tracked",
    kind: "meridian_spawn",
    command: "meridian spawn -a coder -p hi",
  },
);
await emit(handlers, "agent_end", {});
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  {
    subspawn_id: "j-wrap",
    wait_policy: "tracked",
    kind: "meridian_spawn",
    log_path: wrapperLog,
    success: true,
  },
);
for (let attempt = 0; attempt < 20; attempt += 1) {
  const sawChildEnd = rawWrites.some((chunk) =>
    chunk.includes('"subspawn_id":"p123"') &&
    chunk.includes('"meridian.subspawn.end"')
  );
  if (sawChildEnd) {
    break;
  }
  await new Promise((resolve) => setTimeout(resolve, 50));
}
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({ lifecycleEvents, sentMessages }) + "\n");
''',
    )

    child_events = [
        event
        for event in output["lifecycleEvents"]
        if event.get("subspawn_id") == "p123"
    ]
    assert [event["type"] for event in child_events] == [
        "meridian.subspawn.start",
        "meridian.subspawn.end",
    ]
    assert [event["kind"] for event in child_events] == [
        "meridian_spawn",
        "meridian_spawn",
    ]
    assert child_events[0]["wait_policy"] == "tracked"
    assert child_events[1]["status"] == "succeeded"
