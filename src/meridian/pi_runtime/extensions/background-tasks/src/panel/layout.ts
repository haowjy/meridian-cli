const MAX_PROCESS_ROWS = 8;
/** Fixed tail preview in /ps (pi-processes uses 12 padded; stream view is separate). */
export const MAX_LOG_PREVIEW_LINES = 4;

/**
 * Process list: one row per task (up to 8). Log area is a fixed small preview.
 */
export function computePanelLayout(
  terminalRows: number,
  processCount: number,
): { maxVisibleProcesses: number; maxPreviewLines: number } {
  const maxVisibleProcesses = Math.min(Math.max(0, processCount), MAX_PROCESS_ROWS);
  return {
    maxVisibleProcesses,
    maxPreviewLines: MAX_LOG_PREVIEW_LINES,
  };
}
