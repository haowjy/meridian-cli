const MAX_PROCESS_ROWS = 8;

/**
 * Process list uses one row per task (up to 8); remaining height goes to log preview.
 * Unlike pi-processes, we do not pad empty process slots.
 */
export function computePanelLayout(
  terminalRows: number,
  processCount: number,
): { maxVisibleProcesses: number; maxPreviewLines: number } {
  const rows = Math.max(20, terminalRows);
  const maxVisibleProcesses = Math.min(Math.max(0, processCount), MAX_PROCESS_ROWS);
  const chromeLines = 11;
  const processBlock = maxVisibleProcesses;
  const maxPreviewLines = Math.max(3, rows - chromeLines - processBlock);
  return { maxVisibleProcesses, maxPreviewLines };
}
