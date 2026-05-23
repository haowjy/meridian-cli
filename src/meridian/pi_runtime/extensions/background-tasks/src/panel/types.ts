import type { PsRowKind } from "../types";

export type PanelStatus =
  | "running"
  | "exited"
  | "failed"
  | "killed"
  | "timed_out"
  | "terminating"
  | "terminate_timeout"
  | string;

export const LIVE_PANEL_STATUSES = new Set<PanelStatus>([
  "running",
  "terminating",
  "terminate_timeout",
]);

export type PanelEntry = {
  id: string;
  rowKey: string;
  kind: PsRowKind;
  name: string;
  command: string;
  cwd: string;
  pid: number;
  startTime: number;
  endTime: number | null;
  status: PanelStatus;
  exitCode: number | null;
  success: boolean | null;
  combinedLogPath: string;
  logBytes: number;
  persistent: boolean;
  pingIntervalMs: number | null;
  nextPingAtMs: number | null;
  lastActivityAtMs: number | null;
  isLive: boolean;
};
