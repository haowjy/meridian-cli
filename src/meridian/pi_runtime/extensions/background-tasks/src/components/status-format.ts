import type { PanelEntry, PanelStatus } from "../panel/types";

export function statusLabel(entry: PanelEntry): string {
  if (entry.kind === "meridian_spawn") {
    return entry.status;
  }
  switch (entry.status) {
    case "running":
      return "running";
    case "terminating":
      return "terminating";
    case "terminate_timeout":
      return "terminate_timeout";
    case "killed":
      return "killed";
    case "exited":
    case "failed":
      return entry.success ? "exit(0)" : `exit(${entry.exitCode ?? "?"})`;
    case "timed_out":
      return "timed_out";
    default:
      return entry.status;
  }
}

export function statusIcon(status: PanelStatus, success: boolean | null): string {
  switch (status) {
    case "running":
    case "terminating":
      return "\u25CF";
    case "terminate_timeout":
    case "killed":
    case "failed":
      return "\u2717";
    case "exited":
      return success ? "\u2713" : "\u2717";
    case "timed_out":
      return "\u2717";
    default:
      return entryKindIcon(status);
  }
}

function entryKindIcon(status: string): string {
  if (status.toLowerCase().includes("run")) {
    return "\u25CF";
  }
  return "?";
}

export function kindBadge(entry: PanelEntry): string {
  switch (entry.kind) {
    case "meridian_spawn":
      return "spawn";
    default:
      return "task";
  }
}
