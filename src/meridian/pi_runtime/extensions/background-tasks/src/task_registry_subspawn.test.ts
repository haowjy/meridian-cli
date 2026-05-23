import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { createLocalBus } from "../../shared/meridian_event_bus";
import { TaskRegistry } from "./task_registry";

describe("TaskRegistry subspawn events", () => {
  let stateRoot = "";

  afterEach(async () => {
    if (stateRoot) {
      await rm(stateRoot, { recursive: true, force: true });
      stateRoot = "";
    }
  });

  it("emits subspawn start for non-spawn detached tasks", async () => {
    stateRoot = await mkdtemp(path.join(tmpdir(), "bg-tasks-"));
    const bus = createLocalBus();
    const sidecar = { append: vi.fn(), close: vi.fn() };
    const subspawnStarts: Record<string, unknown>[] = [];
    bus.on("meridian:subspawn:start", (payload) => {
      subspawnStarts.push(payload);
    });

    const registry = new TaskRegistry(
      stateRoot,
      "sess-1",
      null,
      bus,
      sidecar,
      { pingIntervalMs: null, pingResetOnActivity: true, defaultPersistent: false },
    );
    await registry.initialize();

    const { runtimeJob } = await registry.startJob(
      "sleep 9",
      "detached",
      process.cwd(),
      { ...process.env } as Record<string, string>,
    );
    await registry.detachJob(runtimeJob.record.task_id);

    expect(subspawnStarts).toHaveLength(1);
    expect(subspawnStarts[0]).toMatchObject({
      subspawn_id: runtimeJob.record.task_id,
      kind: "process",
    });
    expect(sidecar.append).toHaveBeenCalled();

    await registry.shutdownCleanup();
  });
});
