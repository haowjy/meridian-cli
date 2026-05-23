import type { PanelEntry } from "./types";

export function formatDurationMs(ms: number): string {
  const value = Math.max(0, Math.trunc(ms));
  if (value >= 3_600_000) {
    const hours = Math.floor(value / 3_600_000);
    const minutes = Math.floor((value % 3_600_000) / 60_000);
    return minutes > 0 ? `${hours}h${minutes}m` : `${hours}h`;
  }
  if (value >= 60_000) {
    const minutes = Math.floor(value / 60_000);
    const seconds = Math.floor((value % 60_000) / 1000);
    return seconds > 0 ? `${minutes}m${seconds}s` : `${minutes}m`;
  }
  if (value >= 1000) {
    return `${Math.floor(value / 1000)}s`;
  }
  return `${value}ms`;
}

/** Compact ping/persist summary for list rows and status widget. */
export function formatPingBadge(entry: PanelEntry): string {
  if (entry.kind === "meridian_spawn") {
    return "";
  }
  const parts: string[] = [];
  if (entry.persistent) {
    parts.push("persist");
  }
  if (entry.pingIntervalMs != null && entry.pingIntervalMs > 0) {
    parts.push(`ping ${formatDurationMs(entry.pingIntervalMs)}`);
  }
  if (entry.isLive && entry.nextPingAtMs != null) {
    const remaining = entry.nextPingAtMs - Date.now();
    if (remaining > 0) {
      parts.push(`next ${formatDurationMs(remaining)}`);
    }
  }
  return parts.join(" · ");
}

export function formatPingDetailLines(entry: PanelEntry): string[] {
  if (entry.kind === "meridian_spawn") {
    return [];
  }
  const lines: string[] = [];
  lines.push(`persistent: ${entry.persistent ? "yes" : "no"}`);
  if (entry.pingIntervalMs != null) {
    lines.push(`ping interval: ${formatDurationMs(entry.pingIntervalMs)}`);
  }
  if (entry.nextPingAtMs != null) {
    const remaining = entry.nextPingAtMs - Date.now();
    lines.push(
      remaining > 0
        ? `next ping in: ${formatDurationMs(remaining)}`
        : "next ping: due",
    );
  }
  if (entry.lastActivityAtMs != null) {
    lines.push(`last activity: ${formatDurationMs(Date.now() - entry.lastActivityAtMs)} ago`);
  }
  return lines;
}
