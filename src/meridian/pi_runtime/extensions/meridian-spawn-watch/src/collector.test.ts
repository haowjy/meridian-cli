import { describe, expect, it } from "vitest";

import { createLocalBus } from "../../shared/meridian_event_bus";
import { startSpawnCollector } from "./collector";
import { SpawnTreeStore } from "./tree";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

describe("startSpawnCollector", () => {
  it("accepts subspawn_id when kind is meridian_spawn", async () => {
    const dir = await mkdtemp(path.join(tmpdir(), "spawn-collector-"));
    try {
      const tree = new SpawnTreeStore(path.join(dir, "tree.json"));
      const bus = createLocalBus();
      const stop = startSpawnCollector(tree, bus);

      bus.emit("meridian:subspawn:start", {
        kind: "meridian_spawn",
        subspawn_id: "pabc123",
        status: "discovered",
      });
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });

      const file = await tree.read();
      expect(file.nodes).toHaveLength(1);
      expect(file.nodes[0]?.spawn_id).toBe("pabc123");

      stop();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
