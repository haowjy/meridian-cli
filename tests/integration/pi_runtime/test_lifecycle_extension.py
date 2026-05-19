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


def _path_to_esm_url(p: Path) -> str:
    """Convert a path to a file:// URL suitable for Node ESM import().

    On Windows, Node's ESM loader requires file:// URLs for absolute paths.
    On POSIX, raw paths work but file:// URLs are also fine.
    """
    return p.as_uri()


def _run_node_harness(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for Pi runtime extension harness tests")
    if not LIFECYCLE_DIST.is_file():
        pytest.skip("lifecycle extension dist artifact is not built")

    script = tmp_path / "lifecycle-harness.mjs"
    script.write_text(source, encoding="utf-8")
    lifecycle_file = tmp_path / "pi-lifecycle-events.jsonl"
    lifecycle_file.touch()
    env = {
        **os.environ,
        "MERIDIAN_LIFECYCLE_EXTENSION": _path_to_esm_url(LIFECYCLE_DIST),
        "MERIDIAN_TEST_TMP": str(tmp_path),
        "MERIDIAN_PI_LIFECYCLE_EVENT_FILE": str(lifecycle_file),
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
    payload = json.loads(marker_lines[-1])
    lifecycle_events = [
        json.loads(line)
        for line in lifecycle_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if isinstance(payload, dict) and "lifecycleEvents" in payload:
        payload["lifecycleEvents"] = lifecycle_events
    return payload


def test_lifecycle_sidecar_writer_spawned_role_requires_sidecar_env(tmp_path: Path) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
delete process.env.MERIDIAN_PI_LIFECYCLE_EVENT_FILE;
try {
  await import(process.env.MERIDIAN_LIFECYCLE_EXTENSION);
  process.stdout.write("@@RESULT@@" + JSON.stringify({ ok: true }) + "\n");
} catch (error) {
  process.stdout.write(
    "@@RESULT@@" + JSON.stringify({ ok: false, message: String(error?.message ?? error) }) + "\n"
  );
}
''',
    )

    assert output["ok"] is False
    assert "MERIDIAN_PI_LIFECYCLE_EVENT_FILE is required for spawned Pi lifecycle events" in output[
        "message"
    ]


def test_lifecycle_sidecar_writer_spawned_role_fails_when_sidecar_is_unopenable(
    tmp_path: Path,
) -> None:
    # Point at a file inside a non-existent directory — guaranteed unopenable
    # on both POSIX and Windows.  (Using a directory path itself would fail on
    # POSIX with EISDIR but may silently succeed on some Windows/Node combos.)
    unopenable = str(tmp_path / "does-not-exist" / "sidecar.jsonl")
    output = _run_node_harness(
        tmp_path,
        f'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
process.env.MERIDIAN_PI_LIFECYCLE_EVENT_FILE = {json.dumps(unopenable)};
try {{
  await import(process.env.MERIDIAN_LIFECYCLE_EXTENSION);
  process.stdout.write("@@RESULT@@" + JSON.stringify({{ ok: true }}) + "\\n");
}} catch (error) {{
  process.stdout.write(
    "@@RESULT@@" + JSON.stringify({{ ok: false, message: String(error?.message ?? error) }}) + "\\n"
  );
}}
''',
    )

    assert output["ok"] is False
    assert "failed to open lifecycle event file" in output["message"]


def test_lifecycle_sidecar_writer_primary_role_noops_without_sidecar_env(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "primary";
delete process.env.MERIDIAN_PI_LIFECYCLE_EVENT_FILE;
const originalStdoutWrite = process.stdout.write.bind(process.stdout);
const originalStderrWrite = process.stderr.write.bind(process.stderr);
const stdoutWrites = [];
const stderrWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  stdoutWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};
process.stderr.write = (chunk, encoding, callback) => {
  stderrWrites.push(String(chunk));
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
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "j-primary", wait_policy: "tracked", kind: "bash" },
);
await emit(handlers, "agent_end", {});
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  { subspawn_id: "j-primary", wait_policy: "tracked", kind: "bash", success: true },
);
await new Promise((resolve) => setTimeout(resolve, 0));
process.stdout.write = originalStdoutWrite;
process.stderr.write = originalStderrWrite;
originalStdoutWrite(
  "@@RESULT@@"
    + JSON.stringify({
      sentMessages,
      stdoutLines: stdoutWrites.flatMap((chunk) => chunk.split(/\n/)).filter(Boolean),
      stderrLines: stderrWrites.flatMap((chunk) => chunk.split(/\n/)).filter(Boolean),
      lifecycleEvents: [],
    })
    + "\n"
);
''',
    )

    assert len(output["sentMessages"]) == 1
    assert output["stdoutLines"] == []
    assert output["stderrLines"] == []
    assert output["lifecycleEvents"] == []


def test_lifecycle_child_drain_sends_single_wave_aggregate_notification(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
process.env.MERIDIAN_SPAWN_ID = "p-envelope";
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
    notification_events = [
        event
        for event in output["lifecycleEvents"]
        if event["type"].startswith("meridian.notification.")
    ]
    assert notification_events
    for event in notification_events:
        assert event["schema_version"] == 1
        assert event["parent_spawn_id"] == "p-envelope"
        assert isinstance(event["correlation_id"], str)
        assert event["correlation_id"]
        assert isinstance(event["emitted_at_ms"], int)
        assert isinstance(event["notification_id"], str)
        assert event["notification_id"]
    sent = output["sentMessages"]
    assert sent[0]["options"] == {"deliverAs": "followUp", "triggerTurn": True}
    assert sent[0]["message"]["customType"] == "meridian-lifecycle"
    assert sent[0]["message"]["display"] is True
    assert sent[0]["message"]["content"] == (
        "Background work completed. 1 children finished: j-1 succeeded."
    )
    assert sent[0]["message"]["details"]["kind"] == "wave_completed"
    assert sent[0]["message"]["details"]["wave_reason"] == "children_drained"
    assert sent[0]["message"]["details"]["tracked_count"] == 0
    assert sent[0]["message"]["details"]["had_failures"] is False
    assert sent[0]["message"]["details"]["had_timeouts"] is False
    assert sent[0]["message"]["details"]["child_outcomes"] == [
        {"subspawn_id": "j-1", "status": "succeeded", "success": True}
    ]


def test_lifecycle_mixed_success_failure_waits_for_full_wave(tmp_path: Path) -> None:
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
const { default: lifecycle } = await import(
  process.env.MERIDIAN_LIFECYCLE_EXTENSION
);
lifecycle(pi);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "j-ok", wait_policy: "tracked", kind: "bash" },
);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "j-fail", wait_policy: "tracked", kind: "bash" },
);
await emit(handlers, "agent_end", {});
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  {
    subspawn_id: "j-fail",
    wait_policy: "tracked",
    kind: "bash",
    success: false,
    exit_code: 7,
  },
);
await new Promise((resolve) => setTimeout(resolve, 0));
const messagesAfterFailure = sentMessages.length;
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  { subspawn_id: "j-ok", wait_policy: "tracked", kind: "bash", success: true },
);
await new Promise((resolve) => setTimeout(resolve, 0));
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite(
  "@@RESULT@@" +
    JSON.stringify({ sentMessages, lifecycleEvents, messagesAfterFailure }) +
    "\n"
);
''',
    )

    assert output["messagesAfterFailure"] == 0
    assert len(output["sentMessages"]) == 1
    assert output["sentMessages"][0]["message"]["content"] == (
        "Background work completed. 2 children finished: "
        "j-fail failed (exit_code_7); j-ok succeeded."
    )
    details = output["sentMessages"][0]["message"]["details"]
    assert details["kind"] == "wave_completed"
    assert details["wave_reason"] == "children_drained"
    assert details["had_failures"] is True
    assert details["had_timeouts"] is False
    assert details["tracked_count"] == 0
    outcomes = sorted(details["child_outcomes"], key=lambda item: item["subspawn_id"])
    assert outcomes == [
        {"subspawn_id": "j-fail", "status": "failed", "success": False, "reason": "exit_code_7"},
        {"subspawn_id": "j-ok", "status": "succeeded", "success": True},
    ]


def test_lifecycle_duplicate_child_end_first_terminal_outcome_wins(tmp_path: Path) -> None:
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
const { default: lifecycle } = await import(
  process.env.MERIDIAN_LIFECYCLE_EXTENSION
);
lifecycle(pi);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "j-dup", wait_policy: "tracked", kind: "bash" },
);
await emit(handlers, "agent_end", {});
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  { subspawn_id: "j-dup", wait_policy: "tracked", kind: "bash", success: true },
);
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  {
    subspawn_id: "j-dup",
    wait_policy: "tracked",
    kind: "bash",
    success: false,
    exit_code: 9,
  },
);
await new Promise((resolve) => setTimeout(resolve, 0));
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents }) + "\n");
''',
    )

    assert len(output["sentMessages"]) == 1
    outcomes = output["sentMessages"][0]["message"]["details"]["child_outcomes"]
    assert outcomes == [{"subspawn_id": "j-dup", "status": "succeeded", "success": True}]

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="uses a POSIX shell stub/chmod/PATH shim for meridian wrapper interception",
)

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


def test_lifecycle_primary_role_does_not_emit_quiescence_ready_without_tracked_children(
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
    assert ready_events == []
    assert output["sentMessages"] == []


def test_lifecycle_primary_wave_completion_does_not_emit_raw_lifecycle_json_to_stdout(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "primary";
process.env.MERIDIAN_SPAWN_ID = "p-primary-visible";
const fs = await import("node:fs/promises");
const path = await import("node:path");
process.env.MERIDIAN_PI_LIFECYCLE_EVENT_FILE = path.join(
  process.env.MERIDIAN_TEST_TMP,
  "pi-lifecycle-events.jsonl",
);
const originalStdoutWrite = process.stdout.write.bind(process.stdout);
const stdoutWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  stdoutWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};
const originalStderrWrite = process.stderr.write.bind(process.stderr);
const stderrWrites = [];
process.stderr.write = (chunk, encoding, callback) => {
  stderrWrites.push(String(chunk));
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
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "p1567", wait_policy: "tracked", kind: "meridian_spawn" },
);
await emit(handlers, "agent_end", {});
await emit(
  internalHandlers,
  "meridian:subspawn:end",
  { subspawn_id: "p1567", wait_policy: "tracked", kind: "meridian_spawn", success: true },
);
await new Promise((resolve) => setTimeout(resolve, 0));
const lifecycleFileText = await fs.readFile(process.env.MERIDIAN_PI_LIFECYCLE_EVENT_FILE, "utf-8");
const sidecarLifecycleEvents = lifecycleFileText
  .split(/\n/)
  .map((line) => line.trim())
  .filter(Boolean)
  .map((line) => JSON.parse(line));
const stdoutLifecycleLines = stdoutWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter((line) => line.includes("meridian.notification.") || line.includes("parent_spawn_id"));
const stderrLifecycleEvents = stderrWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalStdoutWrite(
  "@@RESULT@@"
    + JSON.stringify({
      sentMessages,
      sidecarLifecycleEvents,
      stdoutLifecycleLines,
      stderrLifecycleEvents,
    })
    + "\n"
);
process.stderr.write = originalStderrWrite;
''',
    )

    assert len(output["sentMessages"]) == 1
    assert output["sentMessages"][0]["options"] == {
        "deliverAs": "followUp",
        "triggerTurn": True,
    }
    details = output["sentMessages"][0]["message"]["details"]
    assert details["child_outcomes"][0]["subspawn_id"] == "p1567"
    sidecar_event_types = [event["type"] for event in output["sidecarLifecycleEvents"]]
    assert sidecar_event_types
    assert "meridian.notification.queued" in sidecar_event_types
    assert "meridian.notification.delivered" in sidecar_event_types
    assert output["stdoutLifecycleLines"] == []
    assert output["stderrLifecycleEvents"] == []

