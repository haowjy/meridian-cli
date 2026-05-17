# qa-validated: pi-rpc-quiescence
"""Executable fake-Pi tests for the managed bash extension contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MANAGED_BASH_DIST = (
    ROOT / "src" / "meridian" / "pi_runtime" / "dist" / "extensions" / "managed-bash" / "index.js"
)


def _run_node_harness(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for Pi runtime extension harness tests")
    if os.name == "nt":
        pytest.skip("managed bash fake-Pi harness exercises POSIX bash semantics")
    if not MANAGED_BASH_DIST.is_file():
        pytest.skip("managed bash extension dist artifact is not built")

    script = tmp_path / "managed-bash-harness.mjs"
    script.write_text(source, encoding="utf-8")
    env = os.environ.copy()
    env["MERIDIAN_MANAGED_BASH_EXTENSION"] = str(MANAGED_BASH_DIST)
    env["MERIDIAN_PI_STATE_DIR"] = str(tmp_path / "pi-state")
    env["MERIDIAN_TEST_CWD"] = str(tmp_path)
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


def test_managed_bash_blocks_short_commands_and_tracks_background_jobs(tmp_path: Path) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
const originalWrite = process.stdout.write.bind(process.stdout);
const rawWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  rawWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};

const { default: managedBash } = await import(process.env.MERIDIAN_MANAGED_BASH_EXTENSION);
const internalEvents = [];
const pi = {
  tools: new Map(),
  events: {
    emit(channel, payload) { internalEvents.push({ channel, payload }); },
    on() { return () => undefined; },
  },
  on() {},
  registerTool(tool) { this.tools.set(tool.name, tool); },
};
await managedBash(pi);
const ctx = {
  cwd: process.env.MERIDIAN_TEST_CWD,
  sessionManager: { getSessionId: () => "ses-test" },
};
const signal = new AbortController().signal;
const bash = pi.tools.get("bash");
const bgWait = pi.tools.get("bash_bg_wait");

const shortResult = await bash.execute(
  "t-short",
  { command: "printf hello", timeout_ms: 1000 },
  signal,
  () => undefined,
  ctx,
);
const trackedResult = await bash.execute(
  "t-bg",
  { command: "sleep 0.1; printf done", timeout_ms: 1 },
  signal,
  () => undefined,
  ctx,
);
const waitResult = await bgWait.execute(
  "t-wait",
  { job_id: trackedResult.details.job_id, timeout_ms: 5000 },
  signal,
  () => undefined,
  ctx,
);
const detachedResult = await bash.execute(
  "t-detached",
  { command: "sleep 0.1", background: true, wait_policy: "detached" },
  signal,
  () => undefined,
  ctx,
);
const detachedWait = await bgWait.execute(
  "t-detached-wait",
  { job_id: detachedResult.details.job_id, timeout_ms: 5000 },
  signal,
  () => undefined,
  ctx,
);

const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
const output = {
  shortResult,
  trackedResult,
  waitResult,
  detachedResult,
  detachedWait,
  lifecycleEvents,
  internalEvents,
};
originalWrite("@@RESULT@@" + JSON.stringify(output) + "\n");
''',
    )

    short = output["shortResult"]
    assert short["details"]["state"] == "exited"
    assert short["details"]["exit_code"] == 0
    assert short["details"]["stdout_tail"] == "hello"

    tracked = output["trackedResult"]
    assert tracked["details"]["state"] == "running"
    assert tracked["details"]["wait_policy"] == "tracked"
    assert output["waitResult"]["details"]["state"] == "exited"
    assert "done" in output["waitResult"]["details"]["log_tail"]

    detached = output["detachedResult"]
    assert detached["details"]["state"] == "running"
    assert detached["details"]["wait_policy"] == "detached"
    assert output["detachedWait"]["details"]["job"]["wait_policy"] == "detached"

    lifecycle_events = output["lifecycleEvents"]
    start_events = [
        event for event in lifecycle_events if event["type"] == "meridian.subspawn.start"
    ]
    end_events = [event for event in lifecycle_events if event["type"] == "meridian.subspawn.end"]
    assert [event["wait_policy"] for event in start_events] == ["tracked", "detached"]
    assert [event["wait_policy"] for event in end_events] == ["tracked", "detached"]


def test_managed_bash_cancel_beats_foreground_timeout_without_detaching(
    tmp_path: Path,
) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
const originalWrite = process.stdout.write.bind(process.stdout);
const rawWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  rawWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};

const { default: managedBash } = await import(process.env.MERIDIAN_MANAGED_BASH_EXTENSION);
const pi = {
  tools: new Map(),
  events: {
    emit() {},
    on() { return () => undefined; },
  },
  on() {},
  registerTool(tool) { this.tools.set(tool.name, tool); },
};
await managedBash(pi);
const ctx = {
  cwd: process.env.MERIDIAN_TEST_CWD,
  sessionManager: { getSessionId: () => "ses-cancel" },
};
const controller = new AbortController();
const bash = pi.tools.get("bash");
const pending = bash.execute(
  "t-cancel",
  { command: "sleep 5", timeout_ms: 5000 },
  controller.signal,
  () => undefined,
  ctx,
);
setTimeout(() => controller.abort(), 20);
const result = await pending;
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({ result, lifecycleEvents }) + "\n");
''',
    )

    result = output["result"]
    assert result["isError"] is True
    assert result["details"]["state"] == "cancelled"
    assert result["details"]["ok"] is False
    assert result["details"]["job"]["status"] == "killed"
    assert [
        event["type"]
        for event in output["lifecycleEvents"]
        if event["type"].startswith("meridian.subspawn.")
    ] == []


def test_managed_bash_bg_list_read_and_kill_contracts(tmp_path: Path) -> None:
    output = _run_node_harness(
        tmp_path,
        r'''
const originalWrite = process.stdout.write.bind(process.stdout);
const rawWrites = [];
process.stdout.write = (chunk, encoding, callback) => {
  rawWrites.push(String(chunk));
  if (typeof encoding === "function") encoding();
  if (typeof callback === "function") callback();
  return true;
};

const { default: managedBash } = await import(process.env.MERIDIAN_MANAGED_BASH_EXTENSION);
const pi = {
  tools: new Map(),
  events: {
    emit() {},
    on() { return () => undefined; },
  },
  on() {},
  registerTool(tool) { this.tools.set(tool.name, tool); },
};
await managedBash(pi);
const ctx = {
  cwd: process.env.MERIDIAN_TEST_CWD,
  sessionManager: { getSessionId: () => "ses-bg-tools" },
};
const signal = new AbortController().signal;
const bash = pi.tools.get("bash");
const bgList = pi.tools.get("bash_bg_list");
const bgRead = pi.tools.get("bash_bg_read");
const bgWait = pi.tools.get("bash_bg_wait");
const bgKill = pi.tools.get("bash_bg_kill");

const loudResult = await bash.execute(
  "t-loud",
  { command: "yes x | head -c 70000", background: true },
  signal,
  () => undefined,
  ctx,
);
const loudJobId = loudResult.details.job_id;
await bgWait.execute(
  "t-loud-wait",
  { job_id: loudJobId, timeout_ms: 5000 },
  signal,
  () => undefined,
  ctx,
);
const loudRead = await bgRead.execute(
  "t-loud-read",
  { job_id: loudJobId, max_bytes: 999999 },
  signal,
  () => undefined,
  ctx,
);
const includeCompletedList = await bgList.execute(
  "t-list-completed",
  { include_completed: true },
  signal,
  () => undefined,
  ctx,
);

const killTarget = await bash.execute(
  "t-kill",
  { command: "sleep 5", background: true },
  signal,
  () => undefined,
  ctx,
);
const killJobId = killTarget.details.job_id;
const runningList = await bgList.execute(
  "t-list-running",
  {},
  signal,
  () => undefined,
  ctx,
);
const killResult = await bgKill.execute(
  "t-kill-job",
  { job_id: killJobId },
  signal,
  () => undefined,
  ctx,
);
const afterKillRunningList = await bgList.execute(
  "t-list-after-kill",
  {},
  signal,
  () => undefined,
  ctx,
);
const lifecycleEvents = rawWrites
  .flatMap((chunk) => chunk.split(/\n/))
  .filter(Boolean)
  .map((line) => JSON.parse(line));
originalWrite("@@RESULT@@" + JSON.stringify({
  loudJobId,
  loudRead,
  includeCompletedList,
  killJobId,
  runningList,
  killResult,
  afterKillRunningList,
  lifecycleEvents,
}) + "\n");
''',
    )

    loud_read = output["loudRead"]
    assert loud_read["details"]["found"] is True
    assert len(loud_read["details"]["data"]) <= 64 * 1024
    assert output["loudJobId"] in {
        job["job_id"] for job in output["includeCompletedList"]["details"]["jobs"]
    }

    kill_job_id = output["killJobId"]
    assert kill_job_id in {
        job["job_id"] for job in output["runningList"]["details"]["jobs"]
    }
    assert output["killResult"]["details"]["found"] is True
    assert output["killResult"]["details"]["job"]["status"] == "killed"
    assert kill_job_id not in {
        job["job_id"] for job in output["afterKillRunningList"]["details"]["jobs"]
    }

    killed_end_events = [
        event
        for event in output["lifecycleEvents"]
        if event["type"] == "meridian.subspawn.end"
        and event["subspawn_id"] == kill_job_id
    ]
    assert killed_end_events
    assert killed_end_events[-1]["status"] == "killed"
