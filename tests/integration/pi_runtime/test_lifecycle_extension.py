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


def test_lifecycle_wrapper_missing_child_id_waits_for_deadline(tmp_path: Path) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
process.env.MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS = "400";
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
await emit(internalHandlers, "meridian:subspawn:start", {
  subspawn_id: "j-wrap-missing",
  wait_policy: "tracked",
  kind: "meridian_spawn",
  command: "meridian spawn -a coder -p hi",
});
await emit(handlers, "agent_end", {});
await emit(internalHandlers, "meridian:subspawn:end", {
  subspawn_id: "j-wrap-missing",
  wait_policy: "tracked",
  kind: "meridian_spawn",
  success: true,
});
await new Promise((resolve) => setTimeout(resolve, 120));
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents }) + "\n");
''',
    )

    assert output["sentMessages"] == []
    queued_reasons = [
        event.get("reason")
        for event in output["lifecycleEvents"]
        if event["type"] == "meridian.notification.queued"
    ]
    assert "children_drained" not in queued_reasons


def test_lifecycle_wave_timeout_marks_tracked_child_and_excludes_detached(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
process.env.MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS = "120";
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
  { subspawn_id: "j-tracked", wait_policy: "tracked", kind: "bash" },
);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "j-detached", wait_policy: "detached", kind: "bash" },
);
await emit(handlers, "agent_end", {});
for (let attempt = 0; attempt < 20; attempt += 1) {
  if (sentMessages.length > 0) break;
  await new Promise((resolve) => setTimeout(resolve, 25));
}
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents }) + "\n");
''',
    )

    assert len(output["sentMessages"]) == 1
    details = output["sentMessages"][0]["message"]["details"]
    assert details["kind"] == "wave_completed"
    assert details["wave_reason"] == "wave_deadline"
    assert details["had_timeouts"] is True
    assert details["had_failures"] is True
    assert details["tracked_count"] == 1
    assert details["child_outcomes"] == [
        {
            "subspawn_id": "j-tracked",
            "status": "timed_out",
            "success": False,
            "reason": "wave_deadline",
        }
    ]


def test_lifecycle_wave_timeout_sends_sigterm_to_tracked_bash_pid_only(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
process.env.MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS = "120";
const originalWrite = process.stdout.write.bind(process.stdout);
const rawWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  rawWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};
const originalKill = process.kill.bind(process);
const aliveByPid = new Map([
  [12345, true],
  [54321, true],
]);
const killCalls = [];
process.kill = (pid, signal) => {
  killCalls.push({ pid, signal: signal ?? null });
  const absolutePid = Math.abs(pid);
  if (signal === 0) {
    if (aliveByPid.get(absolutePid) === true) return true;
    throw Object.assign(new Error("not alive"), { code: "ESRCH" });
  }
  if (signal === "SIGTERM") {
    aliveByPid.set(absolutePid, false);
  }
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
  { subspawn_id: "j-tracked", wait_policy: "tracked", kind: "bash", pid: 12345 },
);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "j-detached", wait_policy: "detached", kind: "bash", pid: 54321 },
);
await emit(handlers, "agent_end", {});
for (let attempt = 0; attempt < 20; attempt += 1) {
  if (sentMessages.length > 0) break;
  await new Promise((resolve) => setTimeout(resolve, 25));
}
process.kill = originalKill;
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite(
  "@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents, killCalls }) + "\n"
);
''',
    )

    assert len(output["sentMessages"]) == 1
    assert output["sentMessages"][0]["message"]["details"]["child_outcomes"] == [
        {
            "subspawn_id": "j-tracked",
            "status": "timed_out",
            "success": False,
            "reason": "wave_deadline",
        }
    ]
    assert {"pid": -12345, "signal": "SIGTERM"} in output["killCalls"]
    assert not any(abs(call["pid"]) == 54321 for call in output["killCalls"])


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="uses a POSIX shell stub/chmod/PATH shim for meridian wrapper interception",
)
def test_lifecycle_wave_timeout_cancels_tracked_meridian_spawn_child(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
process.env.MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS = "120";
const fs = await import("node:fs/promises");
const path = await import("node:path");

const testTmp = process.env.MERIDIAN_TEST_TMP;
const commandLog = path.join(testTmp, "meridian-commands.log");
const binDir = path.join(testTmp, "bin");
await fs.mkdir(binDir, { recursive: true });
const meridian = path.join(binDir, "meridian");
await fs.writeFile(
  meridian,
  "#!/bin/sh\n"
    + "printf '%s\\n' \"$*\" >> \"$MERIDIAN_COMMAND_LOG\"\n"
    + "if [ \"$1\" = \"--json\" ]; then printf '%s\\n' '{\"status\":\"running\"}'; fi\n"
    + "exit 0\n",
  "utf-8",
);
await fs.chmod(meridian, 0o755);
process.env.MERIDIAN_COMMAND_LOG = commandLog;
process.env.PATH = binDir + ":" + process.env.PATH;

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
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "p321", wait_policy: "tracked", kind: "meridian_spawn" },
);
await emit(handlers, "agent_end", {});
for (let attempt = 0; attempt < 24; attempt += 1) {
  if (sentMessages.length > 0) break;
  await new Promise((resolve) => setTimeout(resolve, 25));
}
const commandLines = (await fs.readFile(commandLog, "utf-8"))
  .split(/\n/)
  .map((line) => line.trim())
  .filter(Boolean);
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite(
  "@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents, commandLines }) + "\n"
);
''',
    )

    assert len(output["sentMessages"]) == 1
    assert output["sentMessages"][0]["message"]["details"]["child_outcomes"] == [
        {
            "subspawn_id": "p321",
            "status": "timed_out",
            "success": False,
            "reason": "wave_deadline",
        }
    ]
    assert "spawn cancel p321" in output["commandLines"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="uses a POSIX shell stub/chmod/PATH shim for meridian wrapper interception",
)
def test_lifecycle_wave_timeout_does_not_cancel_detached_meridian_spawn_child(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
process.env.MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS = "120";
const fs = await import("node:fs/promises");
const path = await import("node:path");

const testTmp = process.env.MERIDIAN_TEST_TMP;
const commandLog = path.join(testTmp, "meridian-commands.log");
const binDir = path.join(testTmp, "bin");
await fs.mkdir(binDir, { recursive: true });
const meridian = path.join(binDir, "meridian");
await fs.writeFile(
  meridian,
  "#!/bin/sh\n"
    + "printf '%s\\n' \"$*\" >> \"$MERIDIAN_COMMAND_LOG\"\n"
    + "if [ \"$1\" = \"--json\" ]; then printf '%s\\n' '{\"status\":\"running\"}'; fi\n"
    + "exit 0\n",
  "utf-8",
);
await fs.chmod(meridian, 0o755);
process.env.MERIDIAN_COMMAND_LOG = commandLog;
process.env.PATH = binDir + ":" + process.env.PATH;

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
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "p321", wait_policy: "tracked", kind: "meridian_spawn" },
);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "p654", wait_policy: "detached", kind: "meridian_spawn" },
);
await emit(handlers, "agent_end", {});
for (let attempt = 0; attempt < 24; attempt += 1) {
  if (sentMessages.length > 0) break;
  await new Promise((resolve) => setTimeout(resolve, 25));
}
const commandLines = (await fs.readFile(commandLog, "utf-8"))
  .split(/\n/)
  .map((line) => line.trim())
  .filter(Boolean);
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite(
  "@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents, commandLines }) + "\n"
);
''',
    )

    assert len(output["sentMessages"]) == 1
    assert output["sentMessages"][0]["message"]["details"]["child_outcomes"] == [
        {
            "subspawn_id": "p321",
            "status": "timed_out",
            "success": False,
            "reason": "wave_deadline",
        }
    ]
    cancel_commands = [
        command for command in output["commandLines"] if command.startswith("spawn cancel ")
    ]
    assert cancel_commands == ["spawn cancel p321"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="uses a POSIX shell stub/chmod/PATH shim for meridian wrapper interception",
)
def test_lifecycle_wave_timeout_does_not_cancel_wrapper_discovered_detached_meridian_spawn_child(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
process.env.MERIDIAN_PI_SESSION_ROLE = "spawned";
process.env.MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS = "120";
const fs = await import("node:fs/promises");
const path = await import("node:path");

const testTmp = process.env.MERIDIAN_TEST_TMP;
const commandLog = path.join(testTmp, "meridian-commands.log");
const wrapperLog = path.join(testTmp, "wrapper.log");
await fs.writeFile(wrapperLog, "wrapper discovered child p654\n", "utf-8");
const binDir = path.join(testTmp, "bin");
await fs.mkdir(binDir, { recursive: true });
const meridian = path.join(binDir, "meridian");
await fs.writeFile(
  meridian,
  "#!/bin/sh\n"
    + "printf '%s\\n' \"$*\" >> \"$MERIDIAN_COMMAND_LOG\"\n"
    + "if [ \"$1\" = \"--json\" ]; then printf '%s\\n' '{\"status\":\"running\"}'; fi\n"
    + "exit 0\n",
  "utf-8",
);
await fs.chmod(meridian, 0o755);
process.env.MERIDIAN_COMMAND_LOG = commandLog;
process.env.PATH = binDir + ":" + process.env.PATH;

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
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  { subspawn_id: "p321", wait_policy: "tracked", kind: "meridian_spawn" },
);
await emit(
  internalHandlers,
  "meridian:subspawn:start",
  {
    subspawn_id: "j-wrap",
    wait_policy: "detached",
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
    wait_policy: "detached",
    kind: "meridian_spawn",
    log_path: wrapperLog,
    success: true,
  },
);
for (let attempt = 0; attempt < 24; attempt += 1) {
  if (sentMessages.length > 0) break;
  await new Promise((resolve) => setTimeout(resolve, 25));
}
const commandLines = (await fs.readFile(commandLog, "utf-8"))
  .split(/\n/)
  .map((line) => line.trim())
  .filter(Boolean);
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite(
  "@@RESULT@@" + JSON.stringify({ sentMessages, lifecycleEvents, commandLines }) + "\n"
);
''',
    )

    assert len(output["sentMessages"]) == 1
    details = output["sentMessages"][0]["message"]["details"]
    assert details["child_outcomes"] == [
        {
            "subspawn_id": "p321",
            "status": "timed_out",
            "success": False,
            "reason": "wave_deadline",
        }
    ]
    cancel_commands = [
        command for command in output["commandLines"] if command.startswith("spawn cancel ")
    ]
    assert cancel_commands == ["spawn cancel p321"]
    discovered_child_starts = [
        event
        for event in output["lifecycleEvents"]
        if event.get("type") == "meridian.subspawn.start"
        and event.get("subspawn_id") == "p654"
    ]
    assert len(discovered_child_starts) == 1
    assert discovered_child_starts[0]["wait_policy"] == "detached"


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
process.env.MERIDIAN_SPAWN_ID = "p-wrapper-envelope";
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
    for event in child_events:
        assert event["schema_version"] == 1
        assert event["parent_spawn_id"] == "p-wrapper-envelope"
        assert isinstance(event["correlation_id"], str)
        assert event["correlation_id"]
        assert isinstance(event["emitted_at_ms"], int)
    assert len(output["sentMessages"]) == 1
    details = output["sentMessages"][0]["message"]["details"]
    assert details["kind"] == "wave_completed"
    assert details["wave_reason"] == "children_drained"
    assert details["had_failures"] is False
    assert details["had_timeouts"] is False
    assert details["child_outcomes"] == [
        {"subspawn_id": "p123", "status": "succeeded", "success": True}
    ]
