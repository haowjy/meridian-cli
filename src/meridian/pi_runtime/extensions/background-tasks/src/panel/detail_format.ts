import type { PanelEntry } from "./types";

/** Minimal detail lines under /ps selection (ping policy is in /ps:settings). */
export function formatTaskDetailLines(entry: PanelEntry): string[] {
  const lines: string[] = [];
  if (entry.persistent) {
    lines.push("persistent");
  }
  if (entry.isForeground) {
    lines.push("Foreground $ — ctrl+b or b to background");
  }
  return lines;
}
