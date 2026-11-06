import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { writeFileSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { writeJsonAtomic } from "../../shared/json_file";
import type { BashRecord } from "../../shared/schemas";
import { SpawnWatchRuntime } from "./index";

type Notice = {
  customType: string;
  content: string;
  details: { delivery_id: string; work_ids: string[] };
};

class DeliveryResourceFixture {
  readonly notices: Notice[] = [];
  readonly pi = {
    sendMessage: (message: Notice) => { this.notices.push(message); },
  } as unknown as ExtensionAPI;
  readonly writers: ChildProcess[] = [];
  root = "";
  idle = false;
  runtime: SpawnWatchRuntime | undefined;
  private readonly savedStateDir = process.env._MERIDIAN_PI_STATE_DIR;
  private readonly savedSpawnId = process.env.MERIDIAN_SPAWN_ID;

  async start(): Promise<void> {
    this.root = await mkdtemp(path.join(tmpdir(), "delivery-resources-"));
    process.env._MERIDIAN_PI_STATE_DIR = this.root;
    process.env.MERIDIAN_SPAWN_ID = "p-parent";
    this.runtime = new SpawnWatchRuntime(this.pi, () => this.idle);
    this.runtime.start();
  }

  async child(id: string, overrides: Record<string, unknown> = {}): Promise<void> {
    const canonical = JSON.parse(await readFile(
      new URL("../fixtures/canonical-child-state.json", import.meta.url), "utf8",
    ));
    const terminal = overrides.terminal && typeof overrides.terminal === "object"
      ? { ...canonical.terminal, ...overrides.terminal }
      : canonical.terminal;
    await writeJsonAtomic(path.join(this.root, "spawns", id, "state.json"), {
      ...canonical, id, parent_id: "p-parent", ...overrides, terminal,
    });
  }

  async bash(record: BashRecord): Promise<void> {
    await writeJsonAtomic(path.join(this.root, "pi-bash", "p-parent", "bash-records.json"), {
      v: 1, spawn_id: "p-parent", updated_at_ms: Date.now(), records: { [record.bash_id]: record },
    });
  }

  holdReport(id: string, text = `report for ${id}`): { entered: string; release: string } {
    const report = path.join(this.root, "spawns", id, "report.md");
    const entered = `${report}.entered`;
    const release = `${report}.release`;
    execFileSync("mkfifo", [report]);
    this.writers.push(spawn("bash", ["-c", [
      'exec 3>"$REPORT"', ': >"$ENTERED"',
      'while [[ ! -e "$RELEASE" ]]; do sleep 0.01; done', 'printf %s "$TEXT" >&3',
    ].join("; ")], {
      env: { ...process.env, REPORT: report, ENTERED: entered, RELEASE: release, TEXT: text },
      stdio: "ignore",
    }));
    return { entered, release };
  }

  async cleanup(): Promise<void> {
    this.runtime?.stop();
    for (const writer of this.writers) if (writer.exitCode === null) writer.kill("SIGKILL");
    if (this.root) await rm(this.root, { recursive: true, force: true });
    if (this.savedStateDir === undefined) delete process.env._MERIDIAN_PI_STATE_DIR;
    else process.env._MERIDIAN_PI_STATE_DIR = this.savedStateDir;
    if (this.savedSpawnId === undefined) delete process.env.MERIDIAN_SPAWN_ID;
    else process.env.MERIDIAN_SPAWN_ID = this.savedSpawnId;
  }
}

async function eventually(check: () => boolean | Promise<boolean>): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw Error("delivery resource contract did not settle");
}

async function exists(filePath: string): Promise<boolean> {
  try { await readFile(filePath); return true; }
  catch { return false; }
}

function bashRecord(command: string): BashRecord {
  return {
    bash_id: "b-demo", command, cwd: "/tmp", pid: null, status: "exited",
    is_background: true, is_tracked: true, exit_code: 7,
    started_at_ms: Date.now() - 1000, ended_at_ms: Date.now(), log_path: "/unused",
    stdout_log_path: "/unused", stderr_log_path: "/unused", log_bytes: 0,
    timeout_min: 1,
  };
}

describe("bounded result delivery", () => {
  let fixture: DeliveryResourceFixture;

  beforeEach(async () => {
    fixture = new DeliveryResourceFixture();
    await fixture.start();
  });

  afterEach(async () => fixture.cleanup());

  it("publishes a large backlog in exact member-bounded batches across opportunities", async () => {
    for (let index = 0; index < 35; index++) {
      const id = `p${String(index).padStart(3, "0")}`;
      await fixture.child(id);
      writeFileSync(path.join(fixture.root, "spawns", id, "report.md"), `result ${id}`);
    }

    await fixture.runtime!.deliverAtBoundary();
    expect(fixture.notices).toHaveLength(1);
    expect(fixture.notices[0]!.details.work_ids).toHaveLength(32);
    await fixture.runtime!.admitMessage({ role: "custom", ...fixture.notices[0] });

    await fixture.runtime!.deliverAtBoundary();
    expect(fixture.notices).toHaveLength(2);
    expect(fixture.notices[1]!.details.work_ids).toHaveLength(3);
    await fixture.runtime!.admitMessage({ role: "custom", ...fixture.notices[1] });

    const delivered = fixture.notices.flatMap((notice) => notice.details.work_ids);
    expect(new Set(delivered).size).toBe(35);
    expect(delivered).toEqual(Array.from({ length: 35 }, (_, index) => `p${String(index).padStart(3, "0")}:1`));
    const receipts = JSON.parse(await readFile(
      path.join(fixture.root, "pi-bash", "p-parent", "launch-receipts.json"), "utf8",
    ));
    expect(receipts.messages).toEqual(Object.fromEntries(fixture.notices.map((notice) => [
      notice.details.delivery_id, notice.details.work_ids,
    ])));
  });

  it("shares one packet across 32 large reports and leaves the 33rd owed", async () => {
    const ids = Array.from({ length: 33 }, (_, index) => `p${String(index).padStart(3, "0")}`);
    for (const id of ids) {
      await fixture.child(id);
      writeFileSync(path.join(fixture.root, "spawns", id, "report.md"), "x".repeat(64 * 1024));
    }

    await fixture.runtime!.deliverAtBoundary();
    const first = fixture.notices[0]!;
    const workIds = ids.map((id) => `${id}:1`);
    expect(first.details.work_ids).toEqual(workIds.slice(0, 32));
    expect(Buffer.byteLength(first.content, "utf8")).toBeLessThanOrEqual(64 * 1024);
    for (const id of ids.slice(0, 32)) {
      expect(first.content).toContain(`${id} succeeded (exit 0`);
      expect(first.content).toContain(`meridian spawn report show ${id}`);
      expect(first.content).toContain(`meridian session log ${id}`);
    }
    expect(first.content.match(/Optional spawn details truncated/g)).toHaveLength(32);
    await fixture.runtime!.admitMessage({ role: "custom", ...first });

    await fixture.runtime!.deliverAtBoundary();
    const second = fixture.notices[1]!;
    expect(second.details.work_ids).toEqual([workIds[32]]);
    await fixture.runtime!.admitMessage({ role: "custom", ...second });

    const delivered = fixture.notices.flatMap((notice) => notice.details.work_ids);
    expect(delivered).toEqual(workIds);
    expect(new Set(delivered).size).toBe(33);
    const receipts = JSON.parse(await readFile(
      path.join(fixture.root, "pi-bash", "p-parent", "launch-receipts.json"), "utf8",
    ));
    expect(Object.values(receipts.messages)).toEqual([
      workIds.slice(0, 32), [workIds[32]],
    ]);
  });

  it("queues at most one backlog batch before the next inference opportunity", async () => {
    for (let index = 0; index < 34; index++) {
      const id = `p${String(index).padStart(3, "0")}`;
      await fixture.child(id);
      writeFileSync(path.join(fixture.root, "spawns", id, "report.md"), `result ${id}`);
    }

    fixture.idle = true;
    fixture.runtime!.observeIdle(() => fixture.idle);
    await eventually(() => fixture.notices.length === 1);
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(fixture.notices).toHaveLength(1);
    await fixture.runtime!.admitMessage({ role: "custom", ...fixture.notices[0] });
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(fixture.notices).toHaveLength(1);

    fixture.runtime!.observeIdle(() => fixture.idle);
    await eventually(() => fixture.notices.length === 2);
    expect(fixture.notices[1]!.details.work_ids).toHaveLength(2);
  });

  it("bounds aggregate UTF-8 bytes and advances oversized metadata and reports", async () => {
    await fixture.child("p001", {
      agent: `agent-${"界".repeat(30_000)}`,
      terminal: { error: `failure-${"🔥".repeat(20_000)}` },
    });
    const reportPath = path.join(fixture.root, "spawns", "p001", "report.md");
    writeFileSync(reportPath, `report-${"🧪".repeat(30_000)}`);
    await fixture.child("p002");
    writeFileSync(path.join(fixture.root, "spawns", "p002", "report.md"), "second result");
    await fixture.bash({ ...bashRecord(`printf ${"命".repeat(30_000)}`), bash_id: "b001" });

    await fixture.runtime!.deliverAtBoundary();
    expect(fixture.notices).toHaveLength(1);
    expect(fixture.notices[0]!.details.work_ids).toEqual(["p001:1", "p002:1", "b001"]);
    expect(Buffer.byteLength(fixture.notices[0]!.content, "utf8")).toBeLessThanOrEqual(64 * 1024);
    expect(fixture.notices[0]!.content).toContain("p001 succeeded (exit 0");
    expect(fixture.notices[0]!.content).toContain("Full report: meridian spawn report show p001");
    expect(fixture.notices[0]!.content).toContain("p002 succeeded (exit 0");
    expect(fixture.notices[0]!.content).toContain("Background bash b001 exited (exit 7");
    expect(fixture.notices[0]!.content).toContain('bash_manage({action: "output", bash_id: "b001"})');
    expect(fixture.notices[0]!.content).not.toContain("�");
  });

  it("does not admit a clean-looking outcome when a recorded fault's optional detail cannot fit", async () => {
    const oldMinimum = 'Background bash  exited (exit 0, 0m01s).\n\n[Optional Bash details truncated to fit 65536 UTF-8 bytes.]\nFull output: bash_manage({action: "output", bash_id: ""})';
    const idBytes = Math.floor((64 * 1024 - Buffer.byteLength(oldMinimum)) / 2);
    const id = `b-${"a".repeat(idBytes - 2)}`;
    await fixture.bash({ ...bashRecord("true"), bash_id: id, exit_code: 0,
      // Force the optional body to exceed even the reserved truncation notice. A short
      // diagnostic can fit in full and would not exercise loss of failure evidence.
      started_at_ms: 1000, ended_at_ms: 2000, execution_error: `EIO: required failure evidence ${"detail".repeat(200)}` });
    await fixture.runtime!.deliverAtBoundary();
    // Either the full mandatory outcome fits, or the item must stay owed. Optional text is insufficient.
    for (const notice of fixture.notices) {
      expect(Buffer.byteLength(notice.content)).toBeLessThanOrEqual(64 * 1024);
      expect(notice.content.includes("result error")).toBe(true);
    }
    if (fixture.notices.length === 0) {
      const file = JSON.parse(await readFile(path.join(fixture.root, "pi-bash", "p-parent", "bash-records.json"), "utf8"));
      expect(file.records[id]).toMatchObject({ is_tracked: true, is_background: true, status: "exited" });
      expect(file.records[id].notification_consumed_at_ms).toBeUndefined();
    }
  });

  it("keeps a giant Bash result's identity, outcome, and concrete lookup visible", async () => {
    await fixture.bash(bashRecord("命".repeat(30_000)));

    await fixture.runtime!.deliverAtBoundary();

    const notice = fixture.notices[0]!;
    expect(notice.details.work_ids).toEqual(["b-demo"]);
    expect(Buffer.byteLength(notice.content, "utf8")).toBeLessThanOrEqual(64 * 1024);
    expect(notice.content).toContain("Background bash b-demo exited (exit 7");
    expect(notice.content).toContain('bash_manage({action: "output", bash_id: "b-demo"})');
  });

  it("reserves a visible Bash outcome inside a packet constrained by a preceding spawn", async () => {
    await fixture.child("p000");
    writeFileSync(path.join(fixture.root, "spawns", "p000", "report.md"), "x");
    await fixture.runtime!.deliverAtBoundary();
    const sampleBytes = Buffer.byteLength(fixture.notices[0]!.content, "utf8");
    await fixture.runtime!.admitMessage({ role: "custom", ...fixture.notices[0] });

    const oldGenericSuffix = "[Notification metadata truncated to fit 65536 UTF-8 bytes; use bash_manage(action='output') with the exact work ID from message details.]";
    const remainingBytes = Buffer.byteLength(`\n\n${oldGenericSuffix}`, "utf8");
    await fixture.child("p001");
    writeFileSync(path.join(fixture.root, "spawns", "p001", "report.md"),
      "x".repeat(64 * 1024 - 2 - remainingBytes - (sampleBytes - 1)));
    await fixture.bash(bashRecord("命".repeat(30_000)));

    await fixture.runtime!.deliverAtBoundary();
    expect(fixture.notices[1]!.details.work_ids).toEqual(["p001:1", "b-demo"]);
    expect(Buffer.byteLength(fixture.notices[1]!.content, "utf8")).toBeLessThanOrEqual(64 * 1024);
    expect(fixture.notices[1]!.content).toContain("p001 succeeded (exit 0");
    expect(fixture.notices[1]!.content).toContain("Background bash b-demo exited (exit 7");
    expect(fixture.notices[1]!.content).toContain('bash_manage({action: "output", bash_id: "b-demo"})');
  });

  it("does not start another report after a bounded read is cancelled", async () => {
    await fixture.child("p001");
    await fixture.child("p002");
    const first = fixture.holdReport("p001");
    const second = fixture.holdReport("p002");
    const controller = new AbortController();

    const delivery = fixture.runtime!.deliverAtBoundary(controller.signal);
    await eventually(() => exists(first.entered));
    expect(await exists(second.entered)).toBe(false);
    controller.abort();
    writeFileSync(first.release, "release");
    await delivery;

    expect(await exists(second.entered)).toBe(false);
    expect(fixture.notices).toHaveLength(0);
  });
});
