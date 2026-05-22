import { promises as fs } from "node:fs";
import path from "node:path";

export type SpawnTreeNode = {
  spawn_id: string;
  parent_spawn_id?: string;
  task_id?: string;
  kind: "meridian_spawn" | "meridian_spawn_wrapper";
  status: string;
  label?: string;
  discovered_at_ms: number;
};

export type SpawnTreeFile = {
  nodes: SpawnTreeNode[];
  updated_at_ms: number;
};

export class SpawnTreeStore {
  constructor(private readonly treePath: string) {}

  async read(): Promise<SpawnTreeFile> {
    try {
      const raw = await fs.readFile(this.treePath, "utf-8");
      return JSON.parse(raw) as SpawnTreeFile;
    } catch {
      return { nodes: [], updated_at_ms: Date.now() };
    }
  }

  async write(tree: SpawnTreeFile): Promise<void> {
    await fs.mkdir(path.dirname(this.treePath), { recursive: true });
    const payload: SpawnTreeFile = { ...tree, updated_at_ms: Date.now() };
    await fs.writeFile(this.treePath, `${JSON.stringify(payload, null, 2)}\n`, "utf-8");
  }
}
