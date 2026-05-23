import { runMeridianCommand } from "../../../shared/meridian_cli";
import type { MeridianEventBus } from "../../../shared/meridian_event_bus";
import type { SpawnWatchManager } from "../spawn_manager";
import type { SpawnTreeFile } from "../tree";

function parseStatus(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }
  try {
    const parsed = JSON.parse(trimmed) as { status?: string };
    if (typeof parsed.status === "string") {
      return parsed.status.toLowerCase();
    }
  } catch {
    // fall through
  }
  const match = trimmed.match(/\b(running|queued|starting|finalizing|succeeded|failed|cancelled)\b/i);
  return match?.[1]?.toLowerCase() ?? null;
}

export async function formatSpawnTree(file: SpawnTreeFile): Promise<string> {
  if (file.nodes.length === 0) {
    return "No spawns in tree.";
  }
  const byParent = new Map<string | undefined, typeof file.nodes>();
  for (const node of file.nodes) {
    const key = node.parent_spawn_id;
    const bucket = byParent.get(key) ?? [];
    bucket.push(node);
    byParent.set(key, bucket);
  }
  const lines: string[] = [];
  const walk = (parent: string | undefined, indent: number): void => {
    for (const node of byParent.get(parent) ?? []) {
      lines.push(`${"  ".repeat(indent)}${node.spawn_id} ${node.status} ${node.kind}`);
      walk(node.spawn_id, indent + 1);
    }
  };
  walk(undefined, 0);
  return lines.join("\n");
}

export async function showSpawn(
  bus: MeridianEventBus,
  manager: SpawnWatchManager,
  spawnId: string,
): Promise<string> {
  const result = await runMeridianCommand(["spawn", "show", spawnId, "--no-report"]);
  const status = parseStatus(result.stdout) ?? "unknown";
  const file = await manager.tree.read();
  const node = file.nodes.find((n) => n.spawn_id === spawnId);
  if (node) {
    node.status = status;
    await manager.tree.write(file);
  }
  bus.emit("meridian:spawn:updated", { spawn_id: spawnId, status });
  return (result.stdout || result.stderr).trim() || `spawn ${spawnId}: ${status}`;
}

export async function cancelSpawn(bus: MeridianEventBus, spawnId: string): Promise<string> {
  const result = await runMeridianCommand(["spawn", "cancel", spawnId]);
  bus.emit("meridian:spawn:removed", { spawn_id: spawnId });
  return (result.stdout || result.stderr).trim() || `cancelled ${spawnId}`;
}

export async function waitSpawn(
  bus: MeridianEventBus,
  spawnId: string,
  timeoutMs: number,
): Promise<string> {
  const result = await runMeridianCommand(["spawn", "wait", spawnId], timeoutMs);
  const status = parseStatus(result.stdout) ?? "unknown";
  bus.emit("meridian:spawn:updated", { spawn_id: spawnId, status });
  return (result.stdout || result.stderr).trim() || `wait ${spawnId}: ${status}`;
}
